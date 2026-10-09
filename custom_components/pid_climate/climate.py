"""A climate entity that regulates another climate entity with a PI loop.

The underlying unit's own sensor is unreliable, so this entity closes the loop on
an external room sensor and writes a corrected setpoint to the unit. Heat and cool
are handled by one entity with independent gains, offsets and integrator state.

See README.md; this module implements sections 2, 4, 6 and 7.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timedelta

import voluptuous as vol

from homeassistant.components.climate import (
    PLATFORM_SCHEMA as CLIMATE_PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_UNIQUE_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util

from . import const as c
from .pid import (
    COOL,
    HEAT,
    UNIT_IDLE,
    ModeAwarePI,
    ModeConfig,
    Result,
    auto_off_wanted,
    command_range,
    eight_deg_state,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "PID Climate"

MODE_SCHEMA = vol.Schema(
    {
        vol.Required(c.CONF_KP): vol.Coerce(float),
        vol.Required(c.CONF_KI): vol.Coerce(float),
        vol.Optional(c.CONF_KE, default=0.0): vol.Coerce(float),
        vol.Optional(c.CONF_OFFSET, default=0.0): vol.Coerce(float),
        vol.Optional(c.CONF_INTEGRAL_MIN, default=-4.0): vol.Coerce(float),
        vol.Optional(c.CONF_INTEGRAL_MAX, default=4.0): vol.Coerce(float),
        vol.Optional(c.CONF_AC_MOD_LOWER, default=-1): vol.Coerce(int),
        vol.Optional(c.CONF_AC_MOD_UPPER, default=3): vol.Coerce(int),
        vol.Optional(c.CONF_OVERHEAT_PROTECTION, default=False): cv.boolean,
        vol.Optional(c.CONF_INTEGRAL_BAND, default=0.0): vol.Coerce(float),
        vol.Optional(c.CONF_HOLD_BASE, default=timedelta()): cv.time_period,
        vol.Optional(
            c.CONF_HOLD_PER_DEGREE, default=timedelta(minutes=45)
        ): cv.time_period,
        vol.Optional(c.CONF_HOLD_MAX, default=timedelta(hours=2)): cv.time_period,
        vol.Optional(c.CONF_HOLD_RELEASE_BAND, default=0.3): vol.Coerce(float),
        vol.Optional(c.CONF_AUTO_OFF, default=False): cv.boolean,
        vol.Optional(c.CONF_AUTO_OFF_MARGIN, default=0.5): vol.Coerce(float),
    }
)

PLATFORM_SCHEMA = CLIMATE_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Required(c.CONF_TARGET_ENTITY): cv.entity_id,
        vol.Required(c.CONF_ROOM_SENSOR): cv.entity_id,
        vol.Optional(c.CONF_OUTDOOR_SENSOR): cv.entity_id,
        vol.Optional(c.CONF_HIGH_POWER_SWITCH): cv.entity_id,
        vol.Optional(c.CONF_EIGHT_DEG_SWITCH): cv.entity_id,
        vol.Optional(c.CONF_EIGHT_DEG_THRESHOLD, default=17): vol.Coerce(int),
        vol.Optional(
            c.CONF_EIGHT_DEG_SETTLE, default=timedelta(seconds=5)
        ): cv.time_period,
        vol.Optional(c.CONF_FAN_MODES, default=c.DEFAULT_FAN_MODES): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(c.CONF_FAN_ONLY, default=True): cv.boolean,
        vol.Optional(c.CONF_MIN_TEMP, default=17.0): vol.Coerce(float),
        vol.Optional(c.CONF_MAX_TEMP, default=30.0): vol.Coerce(float),
        vol.Optional(c.CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(c.CONF_TARGET_TEMP_STEP, default=0.5): vol.Coerce(float),
        vol.Optional(c.CONF_COMMAND_MIN, default=17.0): vol.Coerce(float),
        vol.Optional(c.CONF_COMMAND_MAX, default=30.0): vol.Coerce(float),
        vol.Optional(c.CONF_COMMAND_STEP, default=1.0): vol.Coerce(float),
        vol.Optional(
            c.CONF_SAMPLING_PERIOD, default=timedelta(minutes=3)
        ): cv.time_period,
        vol.Optional(
            c.CONF_MIN_SETPOINT_INTERVAL, default=timedelta(minutes=10)
        ): cv.time_period,
        vol.Optional(
            c.CONF_RESYNC_INTERVAL, default=timedelta(minutes=10)
        ): cv.time_period,
        vol.Optional(
            c.CONF_MAX_SAMPLE_GAP, default=timedelta(minutes=15)
        ): cv.time_period,
        vol.Optional(c.CONF_HEAT): MODE_SCHEMA,
        vol.Optional(c.CONF_COOL): MODE_SCHEMA,
    }
)

SET_INTEGRAL_SCHEMA = {
    vol.Required(c.ATTR_VALUE): vol.Coerce(float),
    vol.Optional(c.ATTR_MODE): vol.In([HEAT, COOL]),
}
RESET_INTEGRAL_SCHEMA = {vol.Optional(c.ATTR_MODE): vol.In([HEAT, COOL])}
CLEAR_HOLD_SCHEMA = {vol.Optional(c.ATTR_MODE): vol.In([HEAT, COOL])}


def _mode_config(raw: dict, max_sample_gap: float) -> ModeConfig:
    """Build a controller config. Durations arrive as seconds -- see _storable."""
    return ModeConfig(
        kp=raw[c.CONF_KP],
        ki=raw[c.CONF_KI],
        ke=raw[c.CONF_KE],
        offset=raw[c.CONF_OFFSET],
        integral_min=raw[c.CONF_INTEGRAL_MIN],
        integral_max=raw[c.CONF_INTEGRAL_MAX],
        ac_modulation_lower_margin=raw[c.CONF_AC_MOD_LOWER],
        ac_modulation_upper_margin=raw[c.CONF_AC_MOD_UPPER],
        overheat_protection=raw[c.CONF_OVERHEAT_PROTECTION],
        integral_band=raw[c.CONF_INTEGRAL_BAND],
        hold_base=raw[c.CONF_HOLD_BASE],
        hold_per_degree=raw[c.CONF_HOLD_PER_DEGREE],
        hold_max=raw[c.CONF_HOLD_MAX],
        hold_release_band=raw[c.CONF_HOLD_RELEASE_BAND],
        max_sample_gap=max_sample_gap,
        auto_off=raw[c.CONF_AUTO_OFF],
        auto_off_margin=raw[c.CONF_AUTO_OFF_MARGIN],
    )


def _storable(value):
    """Make validated YAML JSON-safe for a config entry.

    `cv.time_period` yields timedelta objects, which cannot be stored, so every
    duration becomes a float of seconds. Everything downstream reads seconds.
    """
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {k: _storable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_storable(v) for v in value]
    return value


def prepare_import(config: dict) -> dict | None:
    """Turn one validated YAML block into config entry data.

    Shared by first setup and by the reload service, so both produce byte-identical
    data and an unchanged block never triggers a spurious reload.
    """
    if c.CONF_HEAT not in config and c.CONF_COOL not in config:
        _LOGGER.error(
            "%s: at least one of '%s' or '%s' must be configured",
            config[CONF_NAME], c.CONF_HEAT, c.CONF_COOL,
        )
        return None

    data = _storable({k: v for k, v in config.items() if k != CONF_PLATFORM})
    data[CONF_UNIQUE_ID] = config.get(CONF_UNIQUE_ID) or config[CONF_NAME]
    return data


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities, discovery_info=None
) -> None:
    """Hand one YAML block to the import flow.

    No entities are created here. A bare YAML platform has no config entry, and
    without one Home Assistant silently drops `device_info`, so the entities would
    have no device to sit under.
    """
    if (data := prepare_import(config)) is None:
        return

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            c.DOMAIN, context={"source": SOURCE_IMPORT}, data=data
        )
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Create the regulated climate entity for one imported room."""
    config = dict(entry.data)
    gap = config[c.CONF_MAX_SAMPLE_GAP]
    modes: dict[str, ModeConfig] = {}
    for key, mode in ((c.CONF_HEAT, HEAT), (c.CONF_COOL, COOL)):
        if key in config:
            modes[mode] = _mode_config(config[key], gap)
    if not modes:
        _LOGGER.error("%s: no heat or cool block", entry.title)
        return

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        c.SERVICE_SET_INTEGRAL, SET_INTEGRAL_SCHEMA, "async_set_integral"
    )
    platform.async_register_entity_service(
        c.SERVICE_RESET_INTEGRAL, RESET_INTEGRAL_SCHEMA, "async_reset_integral"
    )
    platform.async_register_entity_service(
        c.SERVICE_CLEAR_HOLD, CLEAR_HOLD_SCHEMA, "async_clear_hold"
    )

    entity = PidClimate(config, modes, entry.entry_id)
    hass.data.setdefault(c.DOMAIN, {})[entry.entry_id] = entity
    async_add_entities([entity])


class PidRestoreData(ExtraStoredData):
    """Per-mode controller state, persisted across restarts (README 7)."""

    def __init__(
        self,
        modes: dict[str, dict],
        targets: dict[str, float],
        last_mode: str | None,
        auto_off: bool = False,
    ) -> None:
        self.modes = modes
        self.targets = targets
        self.last_mode = last_mode
        self.auto_off = auto_off

    def as_dict(self) -> dict:
        return {
            "modes": self.modes,
            "targets": self.targets,
            "last_mode": self.last_mode,
            "auto_off": self.auto_off,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PidRestoreData | None:
        data = data or {}
        modes = data.get("modes")
        if not isinstance(modes, dict):
            return None
        targets = data.get("targets")
        return cls(
            modes,
            targets if isinstance(targets, dict) else {},
            data.get("last_mode"),
            bool(data.get("auto_off")),
        )


class PidClimate(ClimateEntity, RestoreEntity):
    """PI regulation of an underlying climate entity."""

    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_has_entity_name = True
    _attr_name = None                # takes the device name: "Salon"
    # The tuning never changes at runtime, so keep it out of the recorder rather
    # than writing ~14 static values per mode into every single state row.
    _unrecorded_attributes = frozenset({f"config_{m}" for m in (HEAT, COOL)})
    # We declare TURN_ON/TURN_OFF explicitly; no-op on HA versions past the
    # migration that introduced this flag.
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, config, modes: dict[str, ModeConfig], key: str) -> None:
        self._key = key
        self._attr_unique_id = config.get(CONF_UNIQUE_ID)
        self._attr_device_info = device_info(config)
        self._attr_min_temp = config[c.CONF_MIN_TEMP]
        self._attr_max_temp = config[c.CONF_MAX_TEMP]
        self._attr_target_temperature_step = config[c.CONF_TARGET_TEMP_STEP]
        self._attr_hvac_modes = [HVACMode.OFF] + [
            HVACMode.HEAT if m == HEAT else HVACMode.COOL for m in modes
        ]
        if config[c.CONF_FAN_ONLY]:
            self._attr_hvac_modes.append(HVACMode.FAN_ONLY)
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = config.get(c.CONF_TARGET_TEMP)

        self._target_entity = config[c.CONF_TARGET_ENTITY]
        self._room_sensor = config[c.CONF_ROOM_SENSOR]
        self._outdoor_sensor = config.get(c.CONF_OUTDOOR_SENSOR)
        self._high_power_switch = config.get(c.CONF_HIGH_POWER_SWITCH)
        self._eight_deg_switch = config.get(c.CONF_EIGHT_DEG_SWITCH)
        self._eight_deg_threshold = config[c.CONF_EIGHT_DEG_THRESHOLD]
        self._eight_deg_settle = config[c.CONF_EIGHT_DEG_SETTLE]
        self._fallback_fan_modes = config[c.CONF_FAN_MODES]

        self._command_min = config[c.CONF_COMMAND_MIN]
        self._command_max = config[c.CONF_COMMAND_MAX]
        self._command_step = config[c.CONF_COMMAND_STEP]

        # Durations arrive from the config entry as seconds.
        self._sampling_period = timedelta(seconds=config[c.CONF_SAMPLING_PERIOD])
        self._min_setpoint_interval = timedelta(
            seconds=config[c.CONF_MIN_SETPOINT_INTERVAL]
        )
        self._resync_interval = timedelta(seconds=config[c.CONF_RESYNC_INTERVAL])

        self._pi = ModeAwarePI(modes)
        # Heat and cool remember their own target, so switching modes does not drag
        # a heating setpoint into cooling. Keyed by mode; `_last_mode` owns the
        # target while the entity is off.
        self._targets: dict[str, float] = {}
        self._last_mode: str = next(iter(modes))
        self._room: float | None = None
        self._outdoor: float | None = None
        self._desired_fan_mode: str | None = None
        self._desired_preset: str = c.PRESET_NONE
        self._last_result: Result | None = None
        self._blocked: str | None = None
        self._auto_off = False           # unit stopped because the command left range
        self._auto_off_applied: bool | None = None
        self._last_setpoint: float | None = None
        self._last_write: datetime | None = None
        self._last_resync: datetime | None = None
        self._writing = asyncio.Lock()

    # -- features ---------------------------------------------------------

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.FAN_MODE
        )
        if self._high_power_switch:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def fan_modes(self) -> list[str]:
        """Mirror the underlying entity, falling back until it exists (README 2)."""
        state = self.hass.states.get(self._target_entity)
        if state and (modes := state.attributes.get(c.ATTR_FAN_MODES)):
            return list(modes)
        return list(self._fallback_fan_modes)

    @property
    def fan_mode(self) -> str | None:
        if self._desired_fan_mode:
            return self._desired_fan_mode
        state = self.hass.states.get(self._target_entity)
        return state.attributes.get(c.ATTR_FAN_MODE) if state else None

    @property
    def preset_modes(self) -> list[str] | None:
        return [c.PRESET_NONE, c.PRESET_BOOST] if self._high_power_switch else None

    @property
    def preset_mode(self) -> str | None:
        """Owned, not mirrored.

        Mirroring the switch meant the unit engaging high power on its own -- which
        Toshiba units do when starting a mode -- silently showed up as `boost`. The
        preset is now ours, defaults to `none`, and is asserted onto the switch on
        every resync, exactly like hvac_mode and fan_mode.
        """
        return self._desired_preset if self._high_power_switch else None

    @property
    def current_temperature(self) -> float | None:
        return self._room

    @property
    def hvac_action(self) -> HVACAction | None:
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._attr_hvac_mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if self._auto_off:
            return HVACAction.IDLE
        result = self._last_result
        if result is None or result.unit_state == UNIT_IDLE:
            return HVACAction.IDLE
        if self._attr_hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        return HVACAction.COOLING

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {
            "target_entity": self._target_entity,
            "auto_off": self._auto_off,
        }
        if self._blocked:
            attrs["control_blocked"] = self._blocked
        for mode in self._pi.modes:
            state = self._pi.state(mode)
            attrs[f"integral_{mode}"] = round(state.integral, 4)
            # The tuning in force for this mode. Durations are in seconds.
            attrs[f"config_{mode}"] = asdict(self._pi.config(mode))
        attrs["setpoint_sent"] = self._last_setpoint
        if self._last_write:
            attrs["last_write"] = self._last_write.isoformat()
        if (r := self._last_result) is not None:
            attrs.update(
                {
                    "error": round(r.error, 3),
                    "pid_p": round(r.p, 3),
                    "pid_i": round(r.i, 3),
                    "pid_e": round(r.e, 3),
                    "setpoint_raw": round(r.raw, 3),
                    "command_min": r.command_min,
                    "command_max": r.command_max,
                    "unit_state": r.unit_state,
                    "integrating": r.integrating,
                    "hold_reason": r.hold_reason,
                    "hold_remaining": round(r.hold_remaining),
                    "sample_dt": round(r.dt, 1),
                }
            )
        return attrs

    # -- lifecycle --------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._room_sensor], self._async_room_changed
            )
        )
        if self._outdoor_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._outdoor_sensor], self._async_outdoor_changed
                )
            )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_interval, self._sampling_period
            )
        )

        self._room = _read_float(self.hass, self._room_sensor)
        if self._outdoor_sensor:
            self._outdoor = _read_float(self.hass, self._outdoor_sensor)

        await self._async_restore()

        if self._attr_target_temperature is None:
            self._attr_target_temperature = self._attr_min_temp

        # Never integrate across the restart gap.
        self._pi.forget_timing()

    async def _async_restore(self) -> None:
        if (last := await self.async_get_last_state()) is not None:
            if last.state in self._attr_hvac_modes:
                self._attr_hvac_mode = HVACMode(last.state)
            if self._attr_target_temperature is None:
                if (temp := last.attributes.get(ATTR_TEMPERATURE)) is not None:
                    self._attr_target_temperature = _as_float(temp, None)
            self._desired_fan_mode = last.attributes.get(c.ATTR_FAN_MODE)
            if (preset := last.attributes.get(c.ATTR_PRESET_MODE)) in (
                c.PRESET_NONE, c.PRESET_BOOST,
            ):
                self._desired_preset = preset

        if (extra := await self.async_get_last_extra_data()) is None:
            return
        stored = PidRestoreData.from_dict(extra.as_dict())
        if stored is None:
            return

        for mode, value in stored.targets.items():
            if mode in self._pi.modes and (temp := _as_float(value, None)) is not None:
                self._targets[mode] = temp
        if stored.last_mode in self._pi.modes:
            self._last_mode = stored.last_mode
        # Restored so the hysteresis picks up where it left off, and so a unit that
        # was stopped is not briefly restarted across the reboot.
        self._auto_off = stored.auto_off
        self._auto_off_applied = stored.auto_off
        # An active mode owns the displayed target; while off, the last one does.
        owner = self._active_mode() or self._last_mode
        if (remembered := self._targets.get(owner)) is not None:
            self._attr_target_temperature = remembered

        for mode, values in stored.modes.items():
            if mode not in self._pi.modes:
                continue
            self._pi.restore(
                mode,
                integral=_as_float(values.get("integral"), 0.0) or 0.0,
                hold_until=_as_float(values.get("hold_until"), None),
                hold_anchor=_as_float(values.get("hold_anchor"), None),
                last_error_sign=int(values.get("last_error_sign") or 0),
            )

    @property
    def extra_restore_state_data(self) -> PidRestoreData:
        return PidRestoreData(
            {
                mode: {
                    "integral": self._pi.state(mode).integral,
                    "hold_until": self._pi.state(mode).hold_until,
                    "hold_anchor": self._pi.state(mode).hold_anchor,
                    "last_error_sign": self._pi.state(mode).last_error_sign,
                }
                for mode in self._pi.modes
            },
            dict(self._targets),
            self._last_mode,
            self._auto_off,
        )

    # -- commands ---------------------------------------------------------

    async def async_set_temperature(self, **kwargs) -> None:
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(HVACMode(mode))
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        old = self._attr_target_temperature
        new = float(temperature)
        # The target belongs to the active mode, or the last active one while off,
        # and so does the hold it arms.
        owner = self._active_mode() or self._last_mode
        if old is not None and new != old:
            self._pi.on_target_change(owner, old, new, self._now())
        self._attr_target_temperature = new
        self._targets[owner] = new
        self.async_write_ha_state()
        # A change you just made should land now, not up to min_setpoint_interval
        # later. The rate limit exists to stop the loop chattering, not to make the
        # UI feel broken.
        await self._async_control(force=True)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self._attr_hvac_modes:
            return
        self._attr_hvac_mode = hvac_mode

        # Restore this mode's own target. No target-change hold is armed: from the
        # incoming mode's point of view the target has not moved since it last ran,
        # and its integral was accumulated against exactly this value.
        if (mode := self._mode_of(hvac_mode)) is not None:
            self._last_mode = mode
            if (remembered := self._targets.get(mode)) is not None:
                self._attr_target_temperature = remembered
            else:
                self._targets[mode] = self._attr_target_temperature

        self._pi.forget_timing()
        self._last_setpoint = None
        self._last_write = None
        self._last_resync = None
        self._last_result = None
        # Re-decided on the next cycle; None forces the mode write either way.
        self._auto_off = False
        self._auto_off_applied = None
        # Published, not just written: off and fan_only return below without ever
        # running a control cycle, so this is the only chance the diagnostics get
        # to hear that the loop stopped.
        self._publish()

        if hvac_mode in (HVACMode.OFF, HVACMode.FAN_ONLY):
            # Neither regulates. Hand the mode over once and stop writing
            # setpoints; the underlying entity is then left alone, exactly as it
            # is while off.
            await self._async_call(
                CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE,
                {ATTR_HVAC_MODE: hvac_mode.value},
            )
            if hvac_mode == HVACMode.FAN_ONLY and self._desired_fan_mode:
                await self._async_call(
                    CLIMATE_DOMAIN, SERVICE_SET_FAN_MODE,
                    {c.ATTR_FAN_MODE: self._desired_fan_mode},
                )
            return
        await self._async_control()

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        for mode in (HVACMode.HEAT, HVACMode.COOL):
            if mode in self._attr_hvac_modes:
                await self.async_set_hvac_mode(mode)
                return

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._desired_fan_mode = fan_mode
        self.async_write_ha_state()
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._async_call(
                CLIMATE_DOMAIN, SERVICE_SET_FAN_MODE, {c.ATTR_FAN_MODE: fan_mode}
            )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if not self._high_power_switch or preset_mode not in (
            c.PRESET_NONE, c.PRESET_BOOST,
        ):
            return
        self._desired_preset = preset_mode
        self.async_write_ha_state()
        await self._async_switch(
            self._high_power_switch, preset_mode == c.PRESET_BOOST
        )

    async def async_set_integral(self, value: float, mode: str | None = None) -> None:
        if (target := mode or self._active_mode()) is None:
            return
        self._pi.set_integral(target, value)
        self._publish()

    async def async_reset_integral(self, mode: str | None = None) -> None:
        """Service: zero the integral for one mode, or all modes."""
        self._pi.reset_integral(mode)
        self._publish()

    async def async_clear_hold(self, mode: str | None = None) -> None:
        """Service: drop the target-change hold, letting integration resume now."""
        self._pi.clear_hold(mode)
        self._publish()

    async def async_force_control(self) -> None:
        """Run one control cycle now, instead of waiting out the sampling period.

        `force=True` for the same reason a target change uses it: an explicit
        manual action should land now rather than at the end of the rate limit. A
        redundant write is still suppressed, so pressing this on a settled loop
        costs nothing but a fresh sample.

        The periodic timer keeps its own schedule; only `dt` shortens for the next
        tick, and since integration is `ki * error * dt` an extra sample merely
        splits the interval rather than adding to the integral.
        """
        await self._async_control(force=True)

    # -- the loop ---------------------------------------------------------

    @callback
    def _async_room_changed(self, event: Event) -> None:
        value = _state_to_float(event.data.get("new_state"))
        if value is None:
            # Drop the reading rather than keep regulating on a frozen one. This is
            # the feedback signal: stale feedback is worse than none, because the
            # loop would happily drive the AC from a temperature that stopped
            # being true an hour ago.
            self._room = None
            self._pi.forget_timing()
            self.async_write_ha_state()
            return

        resumed = self._room is None
        self._room = value
        self.async_write_ha_state()
        if resumed:
            # Do not make the room wait up to a whole sampling period for control
            # to come back after the sensor reappears.
            self.hass.async_create_task(self._async_control())

    @callback
    def _async_outdoor_changed(self, event: Event) -> None:
        # Deliberately keeps the last known value on an outage, unlike the room
        # sensor. Outdoor temperature moves slowly, so a stale reading is a far
        # better feedforward term than silently collapsing `ke * (room - outdoor)`
        # to zero -- which for the salon would drop up to 3.8 °C of command.
        if (value := _state_to_float(event.data.get("new_state"))) is not None:
            self._outdoor = value

    async def _async_interval(self, _now) -> None:
        await self._async_control()

    async def _async_control(self, force: bool = False) -> None:
        mode = self._active_mode()
        if mode is None:
            self._set_blocked(None)          # off is not blocked, just idle
            return
        if self._room is None:
            self._set_blocked("no_room_temperature")
            self._pi.forget_timing()
            return
        if self._attr_target_temperature is None:
            self._set_blocked("no_target_temperature")
            return

        target_state = self.hass.states.get(self._target_entity)
        if target_state is None or target_state.state in (
            STATE_UNAVAILABLE, STATE_UNKNOWN,
        ):
            # Not yet loaded, or offline. Retried every sampling period; the AC
            # keeps whatever setpoint it last received.
            self._set_blocked("target_unavailable")
            self._pi.forget_timing()
            return

        self._set_blocked(None)

        low, high = self._range(mode)
        result = self._pi.step(
            mode=mode,
            room=self._room,
            target=self._attr_target_temperature,
            now=self._now(),
            command_min=low,
            command_max=high,
            outdoor=self._outdoor,
            internal=_as_float(
                target_state.attributes.get(c.ATTR_CURRENT_TEMPERATURE), None
            ),
            ac_setpoint=_as_float(target_state.attributes.get(ATTR_TEMPERATURE), None),
            command_step=self._command_step,
            # Last cycle's decision: whether the unit is stopped right now.
            auto_off_active=self._auto_off,
        )
        self._last_result = result

        # Decide before applying, so the very first cycle after a restart never
        # starts the unit only to stop it again.
        cfg = self._pi.config(mode)
        self._auto_off = cfg.auto_off and auto_off_wanted(
            mode=mode,
            raw=result.raw,
            command_min=result.command_min,
            command_max=result.command_max,
            margin=cfg.auto_off_margin,
            currently_off=self._auto_off,
            command_step=self._command_step,
        )

        await self._async_apply(mode, result, target_state, force)
        self._publish()

    # -- README 6 -----------------------------------------------------------

    def _range(self, mode: str) -> tuple[float, float]:
        return command_range(
            mode=mode,
            command_min=self._command_min,
            command_max=self._command_max,
            eight_deg_threshold=self._eight_deg_threshold,
            has_eight_deg_switch=bool(self._eight_deg_switch),
        )

    def _eight_deg_wanted(self, mode: str, command: float) -> bool | None:
        return eight_deg_state(
            mode=mode,
            command=command,
            eight_deg_threshold=self._eight_deg_threshold,
            has_eight_deg_switch=bool(self._eight_deg_switch),
        )

    async def _async_apply(
        self, mode: str, result: Result, target_state, force: bool = False
    ) -> None:
        """Decide whether to write, then write the minimum that is needed."""
        if self._writing.locked():
            return

        now = dt_util.utcnow()
        reported = _as_float(target_state.attributes.get(ATTR_TEMPERATURE), None)

        resync_due = (
            self._last_resync is None
            or now - self._last_resync >= self._resync_interval
        )
        setpoint_changed = (
            self._last_setpoint is None or result.command != self._last_setpoint
        )
        rate_ok = (
            self._last_write is None
            or now - self._last_write >= self._min_setpoint_interval
        )
        # Someone moved the setpoint out from under us -- correct it on resync
        # regardless of the rate limit, since it is not a change we chose.
        drifted = reported is not None and reported != result.command

        # Stopping or restarting the unit must land now, not at the next resync,
        # and a restart needs its setpoint rewritten immediately afterwards.
        transition = self._auto_off != self._auto_off_applied
        if transition:
            resync_due = True
            force = True
            self._last_setpoint = None

        # `force` (a user target change) skips the rate limit but still will not
        # send a redundant write when the rounded command has not moved. While
        # auto-off holds the unit stopped there is no setpoint worth sending.
        write_setpoint = not self._auto_off and (
            (setpoint_changed and (rate_ok or force)) or (resync_due and drifted)
        )
        if not resync_due and not write_setpoint:
            return

        async with self._writing:
            if resync_due:
                self._last_resync = now
                wanted_mode = (
                    HVACMode.OFF.value
                    if self._auto_off
                    else self._attr_hvac_mode.value
                )
                if target_state.state != wanted_mode:
                    await self._async_call(
                        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE,
                        {ATTR_HVAC_MODE: wanted_mode},
                    )
                wanted_fan = self._desired_fan_mode
                if (
                    wanted_fan
                    and target_state.attributes.get(c.ATTR_FAN_MODE) != wanted_fan
                ):
                    await self._async_call(
                        CLIMATE_DOMAIN, SERVICE_SET_FAN_MODE,
                        {c.ATTR_FAN_MODE: wanted_fan},
                    )
                # The unit engages high power by itself when a mode starts, so put
                # the switch back where we want it rather than adopting its choice.
                if self._high_power_switch:
                    want_boost = self._desired_preset == c.PRESET_BOOST
                    switch = self.hass.states.get(self._high_power_switch)
                    if switch is not None and (switch.state == STATE_ON) != want_boost:
                        await self._async_switch(self._high_power_switch, want_boost)

            if write_setpoint:
                await self._async_write_setpoint(mode, result, now)

        if transition:
            self._auto_off_applied = self._auto_off
            _LOGGER.info(
                "%s: %s (command %.2f, range %g-%g)",
                self.entity_id,
                "stopped the unit, command out of range" if self._auto_off
                else "restarted the unit, command back in range",
                result.raw, result.command_min, result.command_max,
            )

    async def _async_write_setpoint(
        self, mode: str, result: Result, now: datetime
    ) -> None:
        """Write the setpoint, handling the 8 degC switch transition first.

        A switch transition can only be caused by the setpoint crossing the
        threshold, so it is always part of a write that is happening anyway. On a
        transition the unit jumps to a default temperature of its own, hence the
        settle-then-write ordering.
        """
        eight_on = self._eight_deg_wanted(mode, result.command)

        if eight_on is not None and self._eight_deg_switch:
            state = self.hass.states.get(self._eight_deg_switch)
            currently_on = state is not None and state.state == STATE_ON
            if not await self._async_switch(self._eight_deg_switch, eight_on):
                # The unit only accepts 5-16 with 8 °C mode on and 17-30 with it
                # off, so a setpoint written against the wrong state is rejected.
                # Leave the AC alone and retry on the next cycle.
                return
            if currently_on != eight_on:
                await asyncio.sleep(self._eight_deg_settle)

        if await self._async_call(
            CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE,
            {ATTR_TEMPERATURE: result.command},
        ):
            self._last_setpoint = result.command
            self._last_write = now

    async def _async_switch(self, entity_id: str, turn_on: bool) -> bool:
        """Flip a helper switch. Never let a failure abort the control cycle."""
        service = SERVICE_TURN_ON if turn_on else SERVICE_TURN_OFF
        try:
            await self.hass.services.async_call(
                "switch", service, {ATTR_ENTITY_ID: entity_id}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "%s: switch.%s failed on %s", self.entity_id, service, entity_id
            )
            return False
        return True

    async def _async_call(self, domain: str, service: str, data: dict) -> bool:
        try:
            await self.hass.services.async_call(
                domain, service,
                {ATTR_ENTITY_ID: self._target_entity, **data},
                blocking=True,
            )
        except Exception:  # noqa: BLE001 - a failed write must not kill the loop
            _LOGGER.exception(
                "%s: %s.%s failed on %s",
                self.entity_id, domain, service, self._target_entity,
            )
            return False
        return True

    # -- helpers ----------------------------------------------------------

    def _now(self) -> float:
        """Wall clock, so target-change holds survive a restart."""
        return dt_util.utcnow().timestamp()

    @staticmethod
    def _mode_of(hvac_mode: HVACMode) -> str | None:
        if hvac_mode == HVACMode.HEAT:
            return HEAT
        if hvac_mode == HVACMode.COOL:
            return COOL
        return None

    def _active_mode(self) -> str | None:
        return self._mode_of(self._attr_hvac_mode)

    @property
    def active_mode(self) -> str | None:
        """The running mode, or None while off — which the services read as 'all'."""
        return self._active_mode()

    @property
    def control_blocked(self) -> str | None:
        """Which missing input is stopping the loop, or None if it is running."""
        return self._blocked

    def _set_blocked(self, reason: str | None) -> None:
        """Record why the loop is not running, and say so once in the log."""
        if reason == self._blocked:
            return
        if reason is not None:
            _LOGGER.warning(
                "%s: control paused (%s); the AC keeps its last setpoint",
                self.entity_id, reason,
            )
        elif self._blocked is not None:
            _LOGGER.info("%s: control resumed", self.entity_id)
        self._blocked = reason
        # A blocked cycle returns before `_publish`, so dispatch here or the
        # `hold_reason` sensor keeps reporting the last cycle that did run.
        self._publish()

    def _publish(self) -> None:
        self.async_write_ha_state()
        async_dispatcher_send(self.hass, f"{c.SIGNAL_UPDATE}_{self._key}")

    @property
    def last_result(self) -> Result | None:
        return self._last_result

    @property
    def controller(self) -> ModeAwarePI:
        return self._pi

    @property
    def last_setpoint(self) -> float | None:
        return self._last_setpoint


def device_info(config: dict) -> DeviceInfo:
    """One device per regulated room, shared by the climate entity and sensors."""
    return DeviceInfo(
        identifiers={(c.DOMAIN, config[CONF_UNIQUE_ID])},
        name=config[CONF_NAME],
        manufacturer="pid_climate",
        model="PI regulated climate",
    )


def _as_float(value, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _state_to_float(state) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
        return None
    return _as_float(state.state, None)


def _read_float(hass: HomeAssistant, entity_id: str) -> float | None:
    return _state_to_float(hass.states.get(entity_id))
