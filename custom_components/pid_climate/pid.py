"""Mode-aware PI controller for regulating a climate entity by its setpoint.

Deliberately free of Home Assistant imports so the gating logic can be tested on
its own. See README sections 3 and 5; this module implements them.

Sign conventions
----------------
    error   = target - room          (positive => room is too cold)
    command = the setpoint written to the underlying unit

Raising `command` warms the room in *both* heat and cool mode -- in cool mode a
higher setpoint means less cooling -- so the control law is direction agnostic.
Only the *achievability* of a command depends on the mode, which is what the
gating is about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

HEAT = "heat"
COOL = "cool"

# Where the setpoint sits relative to the unit's own reading (README 5.1).
UNIT_UNKNOWN = "unknown"
UNIT_IDLE = "idle"
UNIT_MODULATING = "modulating"
UNIT_FULL_POWER = "full_power"

# Why integration was held this cycle (README 5.2), in evaluation order.
HOLD_NO_DT = "no_time_delta"
HOLD_STALE = "sample_gap_too_long"
HOLD_TARGET_CHANGE = "target_change_hold"
HOLD_UNIT_IDLE = "unit_idle"
HOLD_UNIT_FULL_POWER = "unit_full_power"
HOLD_COMMAND_SATURATED = "command_saturated"
HOLD_OUTSIDE_BAND = "outside_integral_band"
HOLD_CLAMPED = "integral_clamped"


def command_range(
    *,
    mode: str,
    command_min: float,
    command_max: float,
    eight_deg_threshold: float,
    has_eight_deg_switch: bool,
) -> tuple[float, float]:
    """The writable setpoint envelope for a mode.

    8 degC mode is a heating-only feature, so cooling never goes below the
    threshold even though `command_min` is lower to make room for it.

    Deliberately NOT split into 5-16 / 17-30 sub-ranges. Every integer in the
    envelope is writable -- the threshold decides the *switch*, not the range --
    and a sub-range ceiling would read as actuator saturation to the gating in
    §5.2, holding the integral exactly when it needs to grow to cross the
    threshold.
    """
    if has_eight_deg_switch and mode != HEAT:
        return max(command_min, float(eight_deg_threshold)), command_max
    return command_min, command_max


def auto_off_wanted(
    *,
    mode: str,
    raw: float,
    command_min: float,
    command_max: float,
    margin: float,
    currently_off: bool,
) -> bool:
    """Should the unit be stopped because the command left its usable range?

    Heating below `command_min`, or cooling above `command_max`, means we are
    asking for less output than the unit's gentlest setting can deliver. Sitting at
    that setting anyway just overshoots; stopping is the honest action.

    `margin` is one-sided hysteresis on the way back. The command has to climb
    `margin` past the boundary before the unit restarts, so a command hovering on
    the edge cannot flap the compressor. Note this is evaluated on `raw`, not on
    the rounded and clamped command, which by definition can never leave the range.
    """
    if mode == HEAT:
        return raw < (command_min + margin if currently_off else command_min)
    return raw > (command_max - margin if currently_off else command_max)


def eight_deg_state(
    *,
    mode: str,
    command: float,
    eight_deg_threshold: float,
    has_eight_deg_switch: bool,
) -> bool | None:
    """Desired 8 degC switch position for the setpoint about to be written.

    None means there is no switch to touch. Decided from the final rounded
    command, not from `raw`, so the flip happens at the same value the unit is
    actually given. Cooling always turns it off, so a heating session that left it
    on cannot strand the unit in its 5-16 degC range.
    """
    if not has_eight_deg_switch:
        return None
    if mode != HEAT:
        return False
    return command < eight_deg_threshold


def clamp(value: float, low: float, high: float) -> float:
    """Clamp, tolerating low > high (returns low in that case)."""
    if high < low:
        return low
    return max(low, min(high, value))


def round_to_step(value: float, step: float) -> float:
    """Round half away from zero, unlike round() which rounds half to even."""
    if step <= 0:
        return value
    return math.floor(value / step + 0.5) * step


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


@dataclass
class ModeConfig:
    """Per-mode tuning. One instance for heat, one for cool (README 9)."""

    kp: float                          # command degC per degC of error
    ki: float                          # command degC per (degC of error * hour)
    ke: float = 0.0                    # command degC per degC indoor/outdoor delta
    offset: float = 0.0                # feedforward, command degC
    integral_min: float = -4.0         # runaway backstop, not a tuning knob
    integral_max: float = 4.0
    # The two edges of the band in which the unit actually modulates, as signed
    # offsets from its own reading, measured along the axis that increases its
    # output. Integers: the AC reports and accepts whole degrees.
    ac_modulation_lower_margin: int = -1   # below this it is idle
    ac_modulation_upper_margin: int = 3    # above this it is flat out
    overheat_protection: bool = True
    integral_band: float = 0.0         # integrate only within this; 0 disables
    hold_base: float = 0.0             # s, target-change hold
    hold_per_degree: float = 2700.0    # s per degC of accumulated change
    hold_max: float = 7200.0           # s
    hold_release_band: float = 0.3     # degC; arriving inside this frees the hold
    max_sample_gap: float = 900.0      # s; longer gaps do not integrate
    auto_off: bool = False             # stop the unit when the command leaves range
    auto_off_margin: float = 0.5       # degC of hysteresis before restarting


@dataclass
class ModeState:
    """Everything the controller remembers per mode. Persisted (README 7)."""

    integral: float = 0.0
    last_ts: float | None = None       # never restored across a restart
    last_error_sign: int = 0
    hold_until: float | None = None
    hold_anchor: float | None = None    # target in effect when the hold began


@dataclass
class Result:
    command: float              # rounded and clamped, safe to send
    raw: float                  # before rounding and clamping
    command_min: float
    command_max: float
    error: float
    p: float
    i: float
    e: float
    unit_state: str
    dt: float
    integrating: bool
    hold_reason: str | None = None
    hold_remaining: float = 0.0


class ModeAwarePI:
    """A PI controller holding fully independent state per HVAC mode.

    Heat and cool keep separate gains, integrator, timestamp, error sign and
    target-change hold, so switching modes -- or leaving one unused for months --
    never corrupts the other.
    """

    def __init__(self, config: dict[str, ModeConfig]) -> None:
        self._config = dict(config)
        self._state: dict[str, ModeState] = {m: ModeState() for m in self._config}

    # -- state access, for persistence and the services -------------------

    @property
    def modes(self) -> tuple[str, ...]:
        return tuple(self._config)

    def config(self, mode: str) -> ModeConfig:
        return self._config[mode]

    def state(self, mode: str) -> ModeState:
        return self._state[mode]

    def integral(self, mode: str) -> float:
        return self._state[mode].integral

    def set_integral(self, mode: str, value: float) -> None:
        cfg = self._config[mode]
        self._state[mode].integral = clamp(
            float(value), cfg.integral_min, cfg.integral_max
        )

    def reset_integral(self, mode: str | None = None) -> None:
        for key in [mode] if mode else list(self._state):
            self._state[key].integral = 0.0

    def clear_hold(self, mode: str | None = None) -> None:
        """Drop any outstanding target-change hold, releasing integration now."""
        for key in [mode] if mode else list(self._state):
            self._state[key].hold_until = None
            self._state[key].hold_anchor = None

    def forget_timing(self, mode: str | None = None) -> None:
        """Drop the last timestamp so the next cycle cannot integrate.

        Used when control is interrupted: restart, mode change, sensor loss.
        """
        for key in [mode] if mode else list(self._state):
            self._state[key].last_ts = None

    def restore(self, mode: str, *, integral: float, hold_until: float | None,
                hold_anchor: float | None, last_error_sign: int = 0) -> None:
        if mode not in self._state:
            return
        state = self._state[mode]
        cfg = self._config[mode]
        state.integral = clamp(float(integral), cfg.integral_min, cfg.integral_max)
        state.hold_until = hold_until
        state.hold_anchor = hold_anchor
        state.last_error_sign = last_error_sign
        state.last_ts = None

    # -- target changes (README 5.3) ----------------------------------------

    def on_target_change(
        self, mode: str, old_target: float, new_target: float, now: float
    ) -> None:
        """Arm or extend the target-change hold, for one mode only.

        `delta` accumulates from the hold *anchor* rather than from the previous
        target, so a UI slider dragged in 0.5 degC steps behaves like the single
        change it is. The clock restarts on each change while the magnitude
        accumulates; returning to the anchor cancels the hold entirely.

        Strictly per mode. Heat and cool hold independent targets, so a change to
        one says nothing about the other -- arming both would anchor the idle mode
        to a target that is not even its own, and the next change to it would then
        compute `delta` against that stray value.
        """
        state = self._state.get(mode)
        if state is None:
            return
        cfg = self._config[mode]

        held = state.hold_until is not None and now < state.hold_until
        if not held or state.hold_anchor is None:
            state.hold_anchor = old_target

        delta = abs(new_target - state.hold_anchor)
        if delta == 0:
            state.hold_until = None
            state.hold_anchor = None
            return

        duration = min(cfg.hold_base + cfg.hold_per_degree * delta, cfg.hold_max)
        state.hold_until = now + duration

    # -- the control cycle ------------------------------------------------

    def step(
        self,
        *,
        mode: str,
        room: float,
        target: float,
        now: float,
        command_min: float,
        command_max: float,
        outdoor: float | None = None,
        internal: float | None = None,
        ac_setpoint: float | None = None,
        command_step: float = 1.0,
    ) -> Result:
        cfg = self._config[mode]
        state = self._state[mode]

        last_ts, state.last_ts = state.last_ts, now
        dt = 0.0 if last_ts is None else now - last_ts

        error = target - room

        # Overheat protection is independent of the holds below (README 5.4).
        sign = _sign(error)
        if (
            cfg.overheat_protection
            and sign != 0
            and state.last_error_sign != 0
            and sign != state.last_error_sign
        ):
            state.integral /= 2.0
        if sign != 0:
            state.last_error_sign = sign

        # Arriving at the target releases the target-change hold early.
        if state.hold_until is not None and abs(error) <= cfg.hold_release_band:
            state.hold_until = None
            state.hold_anchor = None

        p_term = cfg.kp * error
        e_term = cfg.ke * (room - outdoor) if (cfg.ke and outdoor is not None) else 0.0
        unit_state = self._unit_state(cfg, mode, internal, ac_setpoint)

        def compute(integral: float) -> float:
            return target + cfg.offset + p_term + integral + e_term

        low, high = command_min, command_max
        raw = compute(state.integral)

        hold = self._hold_reason(
            cfg=cfg,
            state=state,
            mode=mode,
            error=error,
            raw=raw,
            low=low,
            high=high,
            dt=dt,
            now=now,
            unit_state=unit_state,
        )

        if hold is None:
            integral = state.integral + cfg.ki * error * dt / 3600.0
            # Still clamped, but silently: reaching the bound is integration that
            # happened, and the next cycle reports HOLD_CLAMPED for as long as the
            # error keeps pushing that way.
            state.integral = clamp(integral, cfg.integral_min, cfg.integral_max)
            raw = compute(state.integral)

        command = clamp(round_to_step(raw, command_step), low, high)
        remaining = (
            max(0.0, state.hold_until - now) if state.hold_until is not None else 0.0
        )

        return Result(
            command=command,
            raw=raw,
            command_min=low,
            command_max=high,
            error=error,
            p=p_term,
            i=state.integral,
            e=e_term,
            unit_state=unit_state,
            dt=dt,
            integrating=hold is None,
            hold_reason=hold,
            hold_remaining=remaining,
        )

    # -- README 5.1 ---------------------------------------------------------

    @staticmethod
    def _unit_state(
        cfg: ModeConfig, mode: str, internal: float | None, ac_setpoint: float | None
    ) -> str:
        """Where the AC's setpoint sits relative to the AC's own reading.

        `demand` is the signed distance along the axis that increases the unit's
        output -- raising the setpoint in heating, lowering it in cooling -- which
        reduces the whole mode mirroring to one sign flip. The configured margins
        are the two edges of the modulating band and belong to it, so the
        comparisons are strict.
        """
        if internal is None or ac_setpoint is None:
            return UNIT_UNKNOWN
        demand = ac_setpoint - internal if mode == HEAT else internal - ac_setpoint
        if demand < cfg.ac_modulation_lower_margin:
            return UNIT_IDLE
        if demand > cfg.ac_modulation_upper_margin:
            return UNIT_FULL_POWER
        return UNIT_MODULATING

    # -- README 5.2 ---------------------------------------------------------

    @staticmethod
    def _hold_reason(
        *,
        cfg: ModeConfig,
        state: ModeState,
        mode: str,
        error: float,
        raw: float,
        low: float,
        high: float,
        dt: float,
        now: float,
        unit_state: str,
    ) -> str | None:
        if dt <= 0:
            return HOLD_NO_DT
        if dt > cfg.max_sample_gap:
            return HOLD_STALE
        if state.hold_until is not None and now < state.hold_until:
            return HOLD_TARGET_CHANGE

        # Hold when the unit is against a limit and the error asks to go past it.
        # Idle: nothing left to give up, so the "less output" direction is dead.
        # Full power: nothing left to give, so the "more output" direction is dead.
        # The opposite direction is always free -- it is what unsticks the unit.
        wants_more = error > 0 if mode == HEAT else error < 0
        wants_less = error < 0 if mode == HEAT else error > 0

        if unit_state == UNIT_IDLE and wants_less:
            return HOLD_UNIT_IDLE

        # Idle and we want more output is deliberately NOT held. Raising the
        # setpoint is what restarts a stalled unit, and observation says it works
        # even when the setpoint stays far below the reported internal
        # temperature -- the unit is evidently deciding from a sensor it does not
        # expose. Do not try to infer reachability from `internal`.
        if unit_state == UNIT_FULL_POWER and wants_more:
            return HOLD_UNIT_FULL_POWER

        # Fallback for when the unit's reading is unavailable: our own clamp.
        if raw > high and error > 0:
            return HOLD_COMMAND_SATURATED
        if raw < low and error < 0:
            return HOLD_COMMAND_SATURATED

        # NOT a classic deadband, which is a neutral zone *around* the setpoint
        # where a controller stops acting. This is the inverse -- conditional
        # integration, sometimes called integral separation: integrate only when
        # close to target, freeze when far, because here the integral's job is to
        # trim a residual bias, not to drive a transient. P does that.
        if cfg.integral_band > 0 and abs(error) > cfg.integral_band:
            return HOLD_OUTSIDE_BAND

        # The same rule once more, on the integral's own bound. `ki` and `dt` are
        # positive, so the sign of the step is the sign of the error -- which makes
        # this the one limit that does not depend on the mode.
        if error > 0 and state.integral >= cfg.integral_max:
            return HOLD_CLAMPED
        if error < 0 and state.integral <= cfg.integral_min:
            return HOLD_CLAMPED

        return None
