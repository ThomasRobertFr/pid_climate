"""Diagnostic sensors exposing the controller's internals (README 2).

Set up from the room's config entry, so there is no `sensor:` YAML block to write.
One set per regulated room, all `entity_category: diagnostic`, sharing the room's
device.

There is deliberately no separate `integrating` sensor: `hold_reason` reads
`integrating` when the loop is accumulating, and names the reason when it is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import const as c
from .climate import PidClimate, device_info
from .pid import UNIT_FULL_POWER, UNIT_IDLE, UNIT_MODULATING, UNIT_UNKNOWN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PidSensorDescription(SensorEntityDescription):
    """A diagnostic value pulled off the climate entity."""

    value: Callable[[PidClimate], float | str | None]


def _result(pid: PidClimate, attr: str, digits: int | None = 3):
    result = pid.last_result
    if result is None:
        return None
    value = getattr(result, attr)
    if digits is None or value is None:
        return value
    return round(value, digits)


# Deliberately no state_class. A state_class makes Home Assistant generate
# long-term statistics, and the history chart then plots those 5-minute (later
# hourly) buckets instead of the raw states -- which hides the 3-minute sampling
# these sensors exist to show. Without it you get every recorded point.
DEGREES = dict(
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
)

SENSORS: tuple[PidSensorDescription, ...] = (
    PidSensorDescription(
        key="error", name="error", **DEGREES,
        value=lambda pid: _result(pid, "error"),
    ),
    PidSensorDescription(
        key="pid_p", name="PID P", **DEGREES,
        value=lambda pid: _result(pid, "p"),
    ),
    PidSensorDescription(
        key="pid_i", name="PID I", **DEGREES,
        value=lambda pid: _result(pid, "i"),
    ),
    PidSensorDescription(
        key="pid_e", name="PID E", **DEGREES,
        value=lambda pid: _result(pid, "e"),
    ),
    PidSensorDescription(
        key="setpoint_raw", name="setpoint raw", **DEGREES,
        value=lambda pid: _result(pid, "raw"),
    ),
    PidSensorDescription(
        key="setpoint_sent", name="setpoint sent", **DEGREES,
        value=lambda pid: pid.last_setpoint,
    ),
    PidSensorDescription(
        key="unit_state", name="unit state",
        device_class=SensorDeviceClass.ENUM,
        options=[UNIT_UNKNOWN, UNIT_IDLE, UNIT_MODULATING, UNIT_FULL_POWER],
        value=lambda pid: _result(pid, "unit_state", None),
    ),
    PidSensorDescription(
        key="hold_reason", name="hold reason",
        value=lambda pid: _hold_reason(pid),
    ),
    PidSensorDescription(
        key="hold_remaining", name="hold remaining",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        value=lambda pid: _result(pid, "hold_remaining", 0),
    ),
    PidSensorDescription(
        key="sample_dt", name="sample interval",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        value=lambda pid: _result(pid, "dt", 1),
    ),
)


# States meaning "the control loop did not run", as opposed to "it ran and chose
# not to integrate". Kept here so the dashboard can tell the two apart.
NOT_RUNNING = ("off", "starting")


def _hold_reason(pid: PidClimate) -> str:
    """Why integration is or is not happening, including when nothing is running.

    Reporting `unknown` while the entity is off made a stopped thermostat
    indistinguishable from a genuine hold on a chart, so the not-running cases get
    named too.
    """
    if pid.active_mode is None:
        return "off"                    # off or fan_only: no loop at all
    if (blocked := pid.control_blocked) is not None:
        return blocked                  # inputs missing, see README §10
    result = pid.last_result
    if result is None:
        return "starting"               # mode selected, first cycle not done
    return result.hold_reason or "integrating"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up diagnostics for one regulated room."""
    pid = hass.data.get(c.DOMAIN, {}).get(entry.entry_id)
    if pid is None:
        # The climate platform is forwarded and awaited first, so this is a bug
        # rather than a race.
        _LOGGER.error("No PID Climate entity registered for %s", entry.title)
        return

    info = device_info(dict(entry.data))
    async_add_entities(
        PidDiagnosticSensor(pid, entry.entry_id, info, description)
        for description in SENSORS
    )


class PidDiagnosticSensor(SensorEntity):
    """One diagnostic value, refreshed whenever the loop publishes."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: PidSensorDescription

    def __init__(
        self,
        pid: PidClimate,
        key: str,
        info: DeviceInfo,
        description: PidSensorDescription,
    ) -> None:
        self._pid = pid
        self._key = key
        self.entity_description = description
        self._attr_device_info = info
        if pid.unique_id:
            self._attr_unique_id = f"{pid.unique_id}_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{c.SIGNAL_UPDATE}_{self._key}", self._async_updated
            )
        )

    @callback
    def _async_updated(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self.entity_description.value(self._pid)
