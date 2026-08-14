"""Tests for pid.py. Run with `python3 test_pid.py` or `pytest test_pid.py`."""

from __future__ import annotations

import sys
from pathlib import Path

# Imported as a bare top-level module, not as `pid_climate.pid`: the package
# __init__ pulls in Home Assistant, and these tests must run without it. pid.py has
# no relative imports, so this works.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "pid_climate")
)

import pid as _pid  # noqa: E402

COOL, HEAT = _pid.COOL, _pid.HEAT
ModeAwarePI, ModeConfig = _pid.ModeAwarePI, _pid.ModeConfig
command_range, eight_deg_state = _pid.command_range, _pid.eight_deg_state
auto_off_wanted = _pid.auto_off_wanted
round_to_step = _pid.round_to_step
UNIT_UNKNOWN, UNIT_IDLE = _pid.UNIT_UNKNOWN, _pid.UNIT_IDLE
UNIT_MODULATING, UNIT_FULL_POWER = _pid.UNIT_MODULATING, _pid.UNIT_FULL_POWER
HOLD_NO_DT, HOLD_STALE = _pid.HOLD_NO_DT, _pid.HOLD_STALE
HOLD_TARGET_CHANGE, HOLD_CLAMPED = _pid.HOLD_TARGET_CHANGE, _pid.HOLD_CLAMPED
HOLD_UNIT_IDLE = _pid.HOLD_UNIT_IDLE
HOLD_UNIT_FULL_POWER = _pid.HOLD_UNIT_FULL_POWER
HOLD_COMMAND_SATURATED = _pid.HOLD_COMMAND_SATURATED
HOLD_OUTSIDE_BAND = _pid.HOLD_OUTSIDE_BAND

COOL_CFG = dict(kp=2.0, ki=2.0, offset=1.0, ac_modulation_lower_margin=-1, ac_modulation_upper_margin=3)
HEAT_CFG = dict(kp=2.0, ki=2.0, offset=-2.5, ac_modulation_lower_margin=-1, ac_modulation_upper_margin=3)

FULL_RANGE = (5.0, 30.0)
NO_EIGHT_DEG = (17.0, 30.0)


def build(mode=COOL, **overrides):
    base = dict(COOL_CFG if mode == COOL else HEAT_CFG)
    base.update(overrides)
    return ModeAwarePI({mode: ModeConfig(**base)})


def cycle(pi, mode, *, room, target, now, internal=None, ac_setpoint=None,
          resolve=NO_EIGHT_DEG, outdoor=None):
    low, high = resolve
    return pi.step(mode=mode, room=room, target=target, now=now,
                   command_min=low, command_max=high, internal=internal,
                   ac_setpoint=ac_setpoint, outdoor=outdoor, command_step=1)


def warm_up(pi, mode, *, room, target, internal=None, ac_setpoint=None,
            resolve=NO_EIGHT_DEG):
    """Burn the first cycle so dt is nonzero on the cycle under test."""
    cycle(pi, mode, room=room, target=target, now=0.0, internal=internal,
          ac_setpoint=ac_setpoint, resolve=resolve)


# -- helpers ---------------------------------------------------------------

def test_round_half_away_from_zero():
    # The bug in the old template setup: `| int` truncated 23.9 to 23.
    assert round_to_step(23.9, 1) == 24
    assert round_to_step(23.5, 1) == 24
    assert round_to_step(22.5, 1) == 23   # round() would give 22 (banker's)
    assert round_to_step(23.4, 1) == 23
    assert round_to_step(23.26, 0.5) == 23.5


def test_ki_is_per_hour():
    # max_sample_gap raised so the 30-minute step is not rejected as stale.
    pi = build(COOL, kp=0.0, offset=0.0, overheat_protection=False,
               max_sample_gap=3600.0)
    warm_up(pi, COOL, room=21.0, target=22.0)
    # 1 degC of error for 30 minutes at ki=2.0 adds exactly 1 degC of command.
    r = cycle(pi, COOL, room=21.0, target=22.0, now=1800.0)
    assert abs(r.i - 1.0) < 1e-9, r.i


# -- README 5.1: unit state classification ----------------------------------

def test_unit_state_heat():
    pi = build(HEAT)
    state = lambda sp, internal: cycle(   # noqa: E731
        pi, HEAT, room=20.0, target=21.0, now=0.0, internal=internal,
        ac_setpoint=sp).unit_state
    # demand = setpoint - internal in heating; the band is [-1, 3] around 25.
    assert state(20, 25) == UNIT_IDLE           # demand -5
    assert state(30, 25) == UNIT_FULL_POWER     # demand +5
    assert state(None, 25) == UNIT_UNKNOWN
    assert state(24, None) == UNIT_UNKNOWN
    # The configured margins are the band's own edges, so they belong to it.
    assert state(24, 25) == UNIT_MODULATING     # demand -1, the lower edge
    assert state(28, 25) == UNIT_MODULATING     # demand +3, the upper edge
    assert state(23, 25) == UNIT_IDLE           # demand -2, one outside
    assert state(29, 25) == UNIT_FULL_POWER     # demand +4, one outside


def test_unit_state_cool_is_mirrored():
    pi = build(COOL)
    state = lambda sp, internal: cycle(   # noqa: E731
        pi, COOL, room=24.0, target=23.0, now=0.0, internal=internal,
        ac_setpoint=sp).unit_state
    # demand = internal - setpoint in cooling, so the same [-1, 3] band mirrors.
    assert state(28, 25) == UNIT_IDLE           # demand -3
    assert state(20, 25) == UNIT_FULL_POWER     # demand +5
    assert state(26, 25) == UNIT_MODULATING     # demand -1, the lower edge
    assert state(22, 25) == UNIT_MODULATING     # demand +3, the upper edge
    assert state(27, 25) == UNIT_IDLE           # demand -2, one outside
    assert state(21, 25) == UNIT_FULL_POWER     # demand +4, one outside


# -- README 5.2: the gating table -------------------------------------------

def test_idle_holds_the_less_output_direction_only():
    # Heat, unit idle. error < 0 (room too warm) asks for less output: dead end.
    pi = build(HEAT)
    warm_up(pi, HEAT, room=22.0, target=21.0, internal=25.0, ac_setpoint=20.0)
    r = cycle(pi, HEAT, room=22.0, target=21.0, now=300.0,
              internal=25.0, ac_setpoint=20.0)
    assert r.unit_state == UNIT_IDLE
    assert r.hold_reason == HOLD_UNIT_IDLE

    # Same state, error > 0: raising the setpoint restarts the unit, so integrate.
    pi = build(HEAT)
    warm_up(pi, HEAT, room=20.0, target=21.0, internal=22.0, ac_setpoint=19.0)
    r = cycle(pi, HEAT, room=20.0, target=21.0, now=300.0,
              internal=22.0, ac_setpoint=19.0)
    assert r.unit_state == UNIT_IDLE
    assert r.integrating, r.hold_reason


def test_full_power_holds_the_more_output_direction_only():
    # Heat, flat out, still too cold: nothing more to give.
    pi = build(HEAT)
    warm_up(pi, HEAT, room=18.0, target=21.0, internal=20.0, ac_setpoint=30.0)
    r = cycle(pi, HEAT, room=18.0, target=21.0, now=300.0,
              internal=20.0, ac_setpoint=30.0)
    assert r.unit_state == UNIT_FULL_POWER
    assert r.hold_reason == HOLD_UNIT_FULL_POWER

    # Flat out but now too warm: backing off does reduce output, so integrate.
    # FULL_RANGE so the low raw setpoint is not caught by the command clamp.
    pi = build(HEAT)
    warm_up(pi, HEAT, room=22.0, target=21.0, internal=20.0, ac_setpoint=30.0,
            resolve=FULL_RANGE)
    r = cycle(pi, HEAT, room=22.0, target=21.0, now=300.0,
              internal=20.0, ac_setpoint=30.0, resolve=FULL_RANGE)
    assert r.unit_state == UNIT_FULL_POWER
    assert r.integrating, r.hold_reason


def test_summer_unreachable_target_is_held():
    """Cool mode, target 26, room 23. Cooling cannot warm a room."""
    pi = build(COOL)
    warm_up(pi, COOL, room=23.0, target=26.0, internal=24.0, ac_setpoint=30.0)
    r = cycle(pi, COOL, room=23.0, target=26.0, now=300.0,
              internal=24.0, ac_setpoint=30.0)
    assert r.error == 3.0
    assert r.unit_state == UNIT_IDLE
    assert r.hold_reason == HOLD_UNIT_IDLE
    assert r.i == 0.0

    # And it stays held right through the slow drift up to the target, which is
    # what a magnitude-based freeze would miss.
    now = 300.0
    for room in [23.5, 24.0, 24.5, 25.0, 25.5, 25.8]:
        now += 300.0
        r = cycle(pi, COOL, room=room, target=26.0, now=now,
                  internal=room + 1.0, ac_setpoint=30.0)
        assert r.hold_reason == HOLD_UNIT_IDLE, (room, r.hold_reason)
    assert r.i == 0.0


def test_heating_coil_leak_still_integrates_upward():
    """internal reads 35 with the room at 20, and that must not block us.

    The unit decides from a sensor it does not expose: bumping the setpoint up
    restarts it even while the setpoint stays far below the reported 35. So the
    "wants more output" direction has to stay open no matter how absurd `internal`
    looks, and the setpoint must actually climb.
    """
    pi = build(HEAT)
    warm_up(pi, HEAT, room=20.0, target=21.0, internal=35.0, ac_setpoint=21.0)
    now, first = 0.0, None
    for _ in range(12):
        now += 300.0
        r = cycle(pi, HEAT, room=20.0, target=21.0, now=now,
                  internal=35.0, ac_setpoint=21.0)
        first = first if first is not None else r.command
    assert r.unit_state == UNIT_IDLE
    assert r.integrating, r.hold_reason
    assert r.i > 0.0, r.i
    assert r.command > first, (first, r.command)


def test_command_saturation_is_the_fallback_without_a_reading():
    pi = build(HEAT, integral_max=100.0)
    warm_up(pi, HEAT, room=15.0, target=25.0)
    r = cycle(pi, HEAT, room=15.0, target=25.0, now=300.0)
    assert r.unit_state == UNIT_UNKNOWN
    assert r.raw > r.command_max
    assert r.hold_reason == HOLD_COMMAND_SATURATED


def test_no_dt_and_stale_gap():
    pi = build(COOL)
    r = cycle(pi, COOL, room=24.0, target=23.0, now=0.0)
    assert r.hold_reason == HOLD_NO_DT
    r = cycle(pi, COOL, room=24.0, target=23.0, now=5000.0)   # > 900s
    assert r.hold_reason == HOLD_STALE


def test_integral_band_disabled_by_default():
    pi = build(COOL)
    warm_up(pi, COOL, room=26.0, target=23.0, internal=26.0, ac_setpoint=24.0)
    r = cycle(pi, COOL, room=26.0, target=23.0, now=300.0,
              internal=26.0, ac_setpoint=24.0)
    assert r.integrating, r.hold_reason

    pi = build(COOL, integral_band=0.5)
    warm_up(pi, COOL, room=26.0, target=23.0, internal=26.0, ac_setpoint=24.0)
    r = cycle(pi, COOL, room=26.0, target=23.0, now=300.0,
              internal=26.0, ac_setpoint=24.0)
    assert r.hold_reason == HOLD_OUTSIDE_BAND


def test_integral_clamp_becomes_a_hold_only_once_parked():
    """Reaching the bound is integration; being asked past it is a hold."""
    pi = build(COOL, integral_max=0.1, integral_min=-0.1)
    warm_up(pi, COOL, room=26.0, target=23.0, internal=26.0, ac_setpoint=25.0)

    # error is -3, so this cycle integrates and lands exactly on the bound. That
    # is integration happening, not a hold, even though the value was clipped.
    r = cycle(pi, COOL, room=26.0, target=23.0, now=300.0,
              internal=26.0, ac_setpoint=25.0)
    assert r.unit_state == UNIT_MODULATING
    assert r.integrating, r.hold_reason
    assert r.i == -0.1

    # Parked on the bound and still asked for more: now it is a hold.
    r = cycle(pi, COOL, room=26.0, target=23.0, now=600.0,
              internal=26.0, ac_setpoint=25.0)
    assert r.hold_reason == HOLD_CLAMPED
    assert r.i == -0.1

    # The bound only blocks the direction it bounds: a sign change frees it.
    r = cycle(pi, COOL, room=22.0, target=23.0, now=900.0,
              internal=22.0, ac_setpoint=21.0)
    assert r.unit_state == UNIT_MODULATING
    assert r.integrating, r.hold_reason
    assert r.i > -0.1


# -- README 5.3: the target-change hold -------------------------------------

def test_half_degree_increments_accumulate_from_the_anchor():
    """21 -> 23 dragged in 0.5 steps must hold for 90 min, not 22.5."""
    pi = build(HEAT)
    now = 0.0
    target = 21.0
    for step in (21.5, 22.0, 22.5, 23.0):
        pi.on_target_change(HEAT, target, step, now)
        target = step
        now += 3.0
    state = pi.state(HEAT)
    held_for = state.hold_until - now
    assert abs(held_for - 90 * 60) < 20, held_for
    assert state.hold_anchor == 21.0


def test_hold_is_capped():
    pi = build(HEAT)
    pi.on_target_change(HEAT, 17.0, 30.0, 0.0)      # 13 degC * 45 min = 9h45
    assert pi.state(HEAT).hold_until == 7200.0


def test_returning_to_the_anchor_cancels_the_hold():
    pi = build(HEAT)
    pi.on_target_change(HEAT, 21.0, 23.0, 0.0)
    assert pi.state(HEAT).hold_until is not None
    pi.on_target_change(HEAT, 23.0, 21.0, 60.0)
    assert pi.state(HEAT).hold_until is None
    assert pi.state(HEAT).hold_anchor is None


def test_hold_blocks_integration_then_releases_on_arrival():
    pi = build(HEAT, overheat_protection=False)
    pi.on_target_change(HEAT, 19.0, 21.0, 0.0)      # 2 degC -> 90 min
    warm_up(pi, HEAT, room=19.0, target=21.0, internal=19.0, ac_setpoint=22.0)

    r = cycle(pi, HEAT, room=19.5, target=21.0, now=600.0,
              internal=19.5, ac_setpoint=22.0)
    assert r.hold_reason == HOLD_TARGET_CHANGE
    assert r.i == 0.0
    assert r.hold_remaining > 4000

    # Arriving inside the release band frees it early, well before 90 minutes.
    r = cycle(pi, HEAT, room=20.9, target=21.0, now=1200.0,
              internal=20.9, ac_setpoint=22.0)
    assert r.integrating, r.hold_reason
    assert r.hold_remaining == 0.0


def test_clear_hold_releases_integration_immediately():
    pi = build(HEAT, overheat_protection=False)
    pi.on_target_change(HEAT, 19.0, 21.0, 0.0)          # 2 degC -> 90 min
    warm_up(pi, HEAT, room=19.0, target=21.0, internal=19.0, ac_setpoint=22.0)

    r = cycle(pi, HEAT, room=19.5, target=21.0, now=600.0,
              internal=19.5, ac_setpoint=22.0)
    assert r.hold_reason == HOLD_TARGET_CHANGE

    pi.clear_hold(HEAT)
    r = cycle(pi, HEAT, room=19.5, target=21.0, now=900.0,
              internal=19.5, ac_setpoint=22.0)
    assert r.integrating, r.hold_reason
    assert r.hold_remaining == 0.0
    assert pi.state(HEAT).hold_anchor is None


def test_clear_hold_defaults_to_every_mode():
    pi = ModeAwarePI({HEAT: ModeConfig(**HEAT_CFG), COOL: ModeConfig(**COOL_CFG)})
    pi.on_target_change(HEAT, 21.0, 23.0, 0.0)
    pi.on_target_change(COOL, 24.0, 22.0, 0.0)
    pi.clear_hold()
    assert pi.state(HEAT).hold_until is None
    assert pi.state(COOL).hold_until is None


def test_hold_applies_only_to_its_own_mode():
    """Heat and cool hold independent targets, so a change to one says nothing
    about the other. Arming both would anchor the idle mode to a target that is
    not its own, and its next change would measure delta against that stray value.
    """
    pi = ModeAwarePI({HEAT: ModeConfig(**HEAT_CFG), COOL: ModeConfig(**COOL_CFG)})
    pi.on_target_change(COOL, 21.5, 22.0, 0.0)
    assert pi.state(COOL).hold_until is not None
    assert pi.state(HEAT).hold_until is None
    assert pi.state(HEAT).hold_anchor is None

    # Heat's own change now anchors at heat's own target, not cooling's 21.5.
    pi.on_target_change(HEAT, 18.5, 19.0, 60.0)
    assert pi.state(HEAT).hold_anchor == 18.5
    held_for = pi.state(HEAT).hold_until - 60.0
    assert abs(held_for - 0.5 * 45 * 60) < 1, held_for


# -- README 5.4 / 7 ---------------------------------------------------------

def test_overheat_protection_halves_on_sign_change():
    # ki=0 isolates the halving from this cycle's own contribution.
    pi = build(COOL, ki=0.0, overheat_protection=True)
    pi.set_integral(COOL, 2.0)
    warm_up(pi, COOL, room=24.0, target=23.0, internal=24.0, ac_setpoint=22.0)
    r = cycle(pi, COOL, room=22.0, target=23.0, now=300.0,
              internal=22.0, ac_setpoint=22.0)
    assert r.i == 1.0, r.i

    pi = build(COOL, ki=0.0, overheat_protection=False)
    pi.set_integral(COOL, 2.0)
    warm_up(pi, COOL, room=24.0, target=23.0, internal=24.0, ac_setpoint=22.0)
    r = cycle(pi, COOL, room=22.0, target=23.0, now=300.0,
              internal=22.0, ac_setpoint=22.0)
    assert r.i == 2.0, r.i


def test_modes_keep_independent_integrals():
    pi = ModeAwarePI({HEAT: ModeConfig(**HEAT_CFG), COOL: ModeConfig(**COOL_CFG)})
    pi.set_integral(HEAT, 1.5)
    pi.set_integral(COOL, -2.0)
    pi.reset_integral(COOL)
    assert pi.integral(HEAT) == 1.5
    assert pi.integral(COOL) == 0.0


def test_restore_never_integrates_across_the_gap():
    pi = build(COOL)
    pi.restore(COOL, integral=1.25, hold_until=None, hold_anchor=None)
    assert pi.integral(COOL) == 1.25
    r = cycle(pi, COOL, room=24.0, target=23.0, now=99999.0)
    assert r.hold_reason == HOLD_NO_DT
    assert r.i == 1.25


def test_eight_deg_switch_follows_the_rounded_command():
    kw = dict(eight_deg_threshold=17, has_eight_deg_switch=True)
    # Heating below the threshold turns it on, at or above turns it off. The
    # boundary is the value actually sent, so 16.6 -> rounds to 17 -> switch off.
    assert eight_deg_state(mode=HEAT, command=16, **kw) is True
    assert eight_deg_state(mode=HEAT, command=5, **kw) is True
    assert eight_deg_state(mode=HEAT, command=17, **kw) is False
    assert eight_deg_state(mode=HEAT, command=round_to_step(16.6, 1), **kw) is False
    assert eight_deg_state(mode=HEAT, command=round_to_step(16.4, 1), **kw) is True
    # Cooling always forces it off, so a heating session cannot strand the unit
    # in its 5-16 range.
    assert eight_deg_state(mode=COOL, command=25, **kw) is False
    # No switch configured means never touch it.
    assert eight_deg_state(
        mode=HEAT, command=16, eight_deg_threshold=17, has_eight_deg_switch=False
    ) is None


def test_auto_off_triggers_below_the_heating_floor():
    kw = dict(command_min=17.0, command_max=30.0, margin=0.5)
    # Evaluated on raw, which is the only value that can leave the range.
    assert auto_off_wanted(mode=HEAT, raw=16.4, currently_off=False, **kw) is True
    assert auto_off_wanted(mode=HEAT, raw=17.2, currently_off=False, **kw) is False
    # Hysteresis: once off, raw must climb past 17.5 to restart.
    assert auto_off_wanted(mode=HEAT, raw=17.2, currently_off=True, **kw) is True
    assert auto_off_wanted(mode=HEAT, raw=17.6, currently_off=True, **kw) is False


def test_auto_off_triggers_above_the_cooling_ceiling():
    kw = dict(command_min=17.0, command_max=30.0, margin=0.5)
    # The summer case: target 26 with the room at 23 drives raw past 30.
    assert auto_off_wanted(mode=COOL, raw=33.0, currently_off=False, **kw) is True
    assert auto_off_wanted(mode=COOL, raw=29.0, currently_off=False, **kw) is False
    assert auto_off_wanted(mode=COOL, raw=29.8, currently_off=True, **kw) is True
    assert auto_off_wanted(mode=COOL, raw=29.4, currently_off=True, **kw) is False


def test_command_range_is_not_split_by_the_threshold():
    """A sub-range ceiling would read as saturation and stall the integral."""
    kw = dict(command_min=5.0, command_max=30.0, eight_deg_threshold=17)
    # Heating gets the whole envelope, so raw 16.4 is nowhere near a ceiling.
    assert command_range(mode=HEAT, has_eight_deg_switch=True, **kw) == (5.0, 30.0)
    # Cooling never goes below the threshold, even though command_min is 5.
    assert command_range(mode=COOL, has_eight_deg_switch=True, **kw) == (17.0, 30.0)
    # Without a switch, command_min is simply the floor.
    assert command_range(mode=HEAT, has_eight_deg_switch=False, **kw) == (5.0, 30.0)


def test_integral_keeps_growing_across_the_eight_deg_threshold():
    """Regression: a 5-16 sub-range used to hold the integral at raw 16.x."""
    pi = build(HEAT, integral_max=10.0)
    low, high = command_range(
        mode=HEAT, command_min=5.0, command_max=30.0,
        eight_deg_threshold=17, has_eight_deg_switch=True,
    )
    # raw = target + offset + kp*error + I = 18.5 - 2.5 + 1.0 + I = 17.0 + I,
    # so I = -1.0 starts the command at exactly 16, just under the threshold.
    pi.set_integral(HEAT, -1.0)
    warm_up(pi, HEAT, room=18.0, target=18.5, resolve=(low, high))
    now, seen, holds = 0.0, [], set()
    for _ in range(20):
        now += 300.0
        r = cycle(pi, HEAT, room=18.0, target=18.5, now=now, resolve=(low, high))
        seen.append(r.command)
        holds.add(r.hold_reason)
    assert 16 in seen and 17 in seen, seen        # crossed the threshold
    assert holds == {None}, holds                # never once held


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as err:
            failed += 1
            print(f"FAIL {test.__name__}: {err}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
