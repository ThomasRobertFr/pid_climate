# pid_climate

A Home Assistant custom integration providing a `climate` entity that regulates
an underlying `climate` entity with a per-mode PI controller closed on an
external room sensor.

## Why this exists

An air conditioner regulates on its own internal sensor, which sits in the indoor
unit rather than in the room. Set it to 24 °C and the room settles somewhere else —
often a degree or two off, varying with load. The usual fix is to put a PID loop on
top, closed on a sensor that is actually in the room, and let it write a corrected
setpoint to the unit.

Two mature integrations already do PID thermostats in Home Assistant. Both are
good; neither quite fits this job.

**[HA Smart Thermostat][hast]** implements a proper PID but drives a *switch*: its
output is a percentage written to a `switch`, `light`, `number` or valve. An AC
accepts a target temperature instead, so using it means adding template sensors to
rescale the output into a setpoint and an automation to write that setpoint to the
climate entity — per room, and again per mode, since one instance is either heating
or cooling. Its anti-windup also freezes the integral on *output saturation*, which
for an AC is the wrong signal: the interesting cases are the target being
unreachable in the current mode (cooling cannot warm a room), the unit sitting
satisfied and idle, or the unit already flat out — none of which show up as the
controller's own output hitting a limit.

**[Versatile Thermostat][vtherm]** does drive climate entities directly and is far
more featureful than this. Its constraint here is that the Expert PID coefficients
are configured globally and shared by every Expert-mode instance, so rooms cannot
be tuned independently, and there is no per-mode feedforward offset.

So this integration:

- writes directly to a `climate` entity — no template sensors, no automations;
- handles heat and cool in **one** entity, with independent gains, feedforward
  offset, integrator state and target for each;
- gates the integral on **what the unit is actually doing** rather than on output
  magnitude, using the unit's own reported temperature and setpoint;
- accommodates the awkward parts of real hardware: a reported temperature that is
  only conditionally meaningful, an "8 °C mode" switch that extends the heating
  setpoint range downward.

It is deliberately narrow. If you are regulating a radiator valve or a relay, use
HA Smart Thermostat. If you want presence detection, window opening, power
management or a UI configuration flow, use Versatile Thermostat.

[hast]: https://github.com/ScratMan/HASmartThermostat
[vtherm]: https://github.com/jmcollin78/versatile_thermostat

## 1. Design rationale

Why the controller is shaped the way it is. The first two points are hardware
quirks — they will not all apply to your unit, but they are the kind of thing the
design has to tolerate.

**The unit's `current_temperature` may be only conditionally trustworthy.** It
nominally reports room temperature, but on at least one unit, in heating, once the
unit is satisfied the fan stops while refrigerant keeps leaking into the indoor
coil — by design, per its manual. The reading then climbs to nonsense: 35 °C with
the room at 20 °C. Consequences:

- It must never be used as the room temperature.
- It *is* usable as an "is the unit doing anything" signal, because the failure
  mode reports high exactly when the unit has stopped, which is the conclusion we
  want. See §5.
- No equivalent failure exists in cooling. The §5 margins are about the width of
  the unit's modulating band, which is a separate matter, so they are the same in
  both modes.

**Units generally report and accept whole degrees.** Where `current_temperature`
and `temperature` on the underlying entity are integers, the §5 margins are
integers too — a fractional margin would be false precision.

**The integral legitimately carries several degrees, and the amount moves.** A
modulating unit needs a standing gap between its internal reading and its
setpoint to deliver steady output, and that gap grows with heat load. So the
integrator absorbs a load term that varies with outdoor temperature and solar
gain — not just a fixed sensor bias. Therefore:

- `integral_min`/`integral_max` are a runaway backstop, not a tuning knob.
- Windup prevention cannot be magnitude-based. It keys off whether the unit can
  actually act (§5).

The `offset` you measure — set the unit to a fixed command, wait for equilibrium,
take `command - room` — is an *equilibrium* figure bundling sensor bias and typical
load. It is feedforward, not a physical constant, and is equivalent to a fixed
recalibration of the unit's own temperature sensor.

## 2. Entity model

One **device** per regulated room, carrying one `climate` entity plus its
diagnostics — eleven entities that would otherwise be loose in a flat list.

Configuration is YAML only; there is no interactive config flow and no UI setup
dialog. Each block is imported into a config entry behind the scenes so that the
entities can belong to a device — see §12.

**Reloading.** `pid_climate.reload` re-reads `configuration.yaml`, revalidates each
block and pushes it back through the import flow. An entry whose data is unchanged
is left running untouched, so reloading with no edits costs nothing; only edited
rooms reload, and those lose their in-memory integral, hold and per-mode targets
since the entity is rebuilt. New blocks create entries. Blocks that disappeared are
**reported, not deleted** — removing an entry would throw away its stored state, and
a block is as likely to be commented out for five minutes as retired for good.

Entities use `has_entity_name`, so the climate entity takes the device's name and
the diagnostics are prefixed with it — a device named "Living Room" gives
`climate.living_room` and a sensor reading "Living Room PID I".

| Capability | Behaviour |
|---|---|
| `hvac_modes` | `off` plus whichever of `heat` / `cool` is configured, plus `fan_only` when enabled |
| `target_temperature` | user-facing, own `min_temp` / `max_temp` / `target_temp_step`; **remembered per mode**, see below |
| `current_temperature` | from `room_sensor`, never from the underlying unit |
| `hvac_action` | `off` / `fan` / `heating` / `cooling` / `idle`, `idle` from §5's detector |
| `fan_mode` | `fan_modes` mirrored from the underlying entity; pass-through |
| `preset_mode` | `none` / `boost`, driving `high_power_switch` — owned, not mirrored |

Mode selection is manual only. No auto heat/cool switching, ever.

**`fan_only`** is offered by default. Set the entity-level `fan_only: false` to hide
it on a unit that does not support the mode — selecting it there would fail at the
service call, logged and otherwise harmless. Nothing is regulated in `fan_only`:
the mode (and the current fan speed) is
handed to the unit once and no setpoint is ever written, so the underlying entity is
then left alone exactly as it is while off — including no resync, so a change made
on the unit's own remote is not overwritten. `hvac_action` reports `fan`. The target
temperature is still settable and is remembered against the last regulating mode,
so returning to `heat` or `cool` picks that mode's target back up.

**Per-mode target.** Heat and cool each remember their own target, restored on mode
switch, so a heating setpoint is never dragged into cooling — the two commonly
differ by several degrees. While the entity is off, the last active mode owns the displayed
target. Switching modes does **not** arm the §5.3 hold: from the incoming mode's
point of view the target has not moved since it last ran, and its integral was
accumulated against exactly that value.

`preset_mode` is **owned**, defaults to `none`, is restored across restarts, and is
asserted onto `high_power_switch` on every resync — the same treatment as
`hvac_mode` and `fan_mode`, and for the same reason. §6.2's rule is that while
regulating we own the underlying entity's state and overwrite external changes;
mirroring the switch would make this the one attribute that adopts them instead.
It would also mean the entity reports one value and then silently changes it at the
next resync, and that a switch reporting `unavailable` at startup leaves
`preset_mode` blank rather than restoring what you last chose. Flip the switch
directly and the next resync puts it back.

If the underlying entity is not loaded at startup, `fan_modes` falls back to the
configured `fan_modes` list, replaced by the underlying entity's own list as soon
as it appears. **The fallback strings must match the AC's one exactly**,
capitalisation included, or writes will fail silently.

**Button entities** (`entity_category: config`, on the room's device)

*Reset integral* and *Clear hold*, both acting on the active mode — or on every
mode while the entity is off, since there is no active one to pick.

*Force update* runs one control cycle immediately instead of waiting out
`sampling_period`, and writes the resulting setpoint without waiting out
`min_setpoint_interval` — the same exemption a target change gets, since both are
explicit manual actions. A redundant write is still suppressed, so pressing it on a
settled loop only refreshes the diagnostics. It does nothing while the entity is
`off` or in `fan_only`, where no loop runs. Useful after a tuning reload, or to see
the effect of a change without watching the clock.

**Diagnostic entities** (`entity_category: diagnostic`, on the room's device)

`error`, `pid_p`, `pid_i`, `pid_e`, `setpoint_raw` (pre-rounding, pre-clamp),
`setpoint_sent`, `unit_state` (`idle` / `modulating` / `full_power` / `unknown`),
`hold_reason`, `hold_remaining`, `sample_dt`.

`hold_reason` distinguishes **the loop ran and declined to integrate** from **the
loop did not run**, which are otherwise indistinguishable on a chart:

| Value | Meaning |
|---|---|
| `integrating` | Running and accumulating |
| a §5.2 reason | Running, integration held |
| `off` | Entity is `off` or `fan_only` — no loop at all |
| `starting` | Mode selected, first cycle not finished |
| a §10 blocked reason | An input is missing |

The same values are also attributes on the climate entity, so one `state_attr`
call pulls the lot for a template.

The climate entity additionally carries `config_heat` / `config_cool` — the full
`ModeConfig` in force for each configured mode, so the tuning is visible next to
the values it produced without cross-referencing YAML. Durations are in seconds.
Both are listed in `_unrecorded_attributes`: they never change at runtime, and
writing ~15 static values per mode into every state row is pure recorder waste.

None of these sensors declare a `state_class`. A `state_class` makes Home Assistant
generate long-term statistics, and its history chart then plots those 5-minute
(later hourly) buckets rather than the raw states — which hides the very sampling
cadence these sensors exist to show. Without it, every recorded point is plotted,
at the cost of no long-term statistics retention.

Note that Home Assistant only records a new state when the value *changes*, so a
flat `pid_i` or a steady `sample_dt` produces no new history points however often
the loop publishes.

## 3. Control law

All terms are in **degrees of AC command**. No ×10 scaling.

```
error    = target - room                      (positive => room too cold)
p        = kp * error
e        = ke * (room - outdoor)
raw      = target + offset + p + integral + e
setpoint = clamp(round_to_step(raw, command_step), effective_min, effective_max)
```

Raising the setpoint warms the room in both modes (in cooling it means less
cooling), so one sign convention covers both. `ke > 0` is correct for both modes:
cold outside raises the heating setpoint, hot outside lowers the cooling setpoint.

`integral += ki * error * dt` when not held (§5), then clamped to
`[integral_min, integral_max]`.

Heat and cool hold **independent** gains, offset, integrator state, target-change
hold timer, and last-sample timestamp. Switching modes never touches the other
mode's state, and a mode unused for months cannot integrate across the gap on its
first cycle back.

`effective_min` / `effective_max` come from §6's 8 °C mode handling.

## 4. Sampling

`sampling_period` (default 3 min) — how often the loop computes. Independent of
how often it writes (§6). `dt` is measured, not assumed; a gap longer than
`max_sample_gap` (default 15 min) skips integration for that cycle.

## 5. Integration gating (anti-windup)

The integral is **frozen, never zeroed**, whenever accumulating cannot help.

### 5.1 The unit's response band

The underlying unit only responds to setpoint changes within a band around its
own reading. Let `internal` be its `current_temperature` and `ac_setpoint` its
reported `temperature` (falling back to our last written value). In **heat**:

```
demand = ac_setpoint - internal   (heat)
demand = internal - ac_setpoint   (cool)

demand < ac_modulation_lower_margin  -> idle
        within the band              -> modulating
demand > ac_modulation_upper_margin  -> full power
```

`demand` is the signed distance along the axis that increases the unit's output —
raising the setpoint in heating, lowering it in cooling — so the mode mirroring is
one sign flip rather than two separately-named margins.

The two parameters are **the edges of the modulating band**, not distances to the
other states, so they belong to the band and the comparisons are strict.
`-1` / `3` reads directly as "still doing a little one degree past satisfied, flat
out beyond three". With `internal = 20` in heating: idle ≤ 18, modulating 19–23,
full power ≥ 24. Cooling with `internal = 24`: full power ≤ 20, modulating 21–25,
idle ≥ 26. The integral accumulates only in the `modulating`
band, and only in a direction the unit can act on. Reported as `unit_state`.

Under the heating leak failure a spuriously high `internal` reads as `idle`,
which is correct — the fan has stopped. It cannot produce a false `full_power`.

**`internal` says what the unit is doing, never what it is able to do.** A unit
reporting 35 °C in heating with the room at 20 °C still restarts when the setpoint
is nudged up by 1 °C, far below 35 — it evidently decides from a sensor it does not
expose. So the band model must not be extended into a reachability test: the "wants
more output" direction stays open unconditionally, because raising the setpoint is
what unsticks a stalled unit.

Otherwise the reading tracks the unit's behaviour closely, which is what §5.1
encodes: heating hard reads well below the setpoint, barely heating reads about
equal to it.

### 5.2 Hold reasons

First match wins; exposed as `hold_reason`.

1. **`no_time_delta`** — first cycle, or `dt <= 0`.
2. **`sample_gap_too_long`** — `dt > max_sample_gap`.
3. **`target_change_hold`** — see §5.3.
4. **`unit_idle`** — `unit_state == idle` and the error asks for **less** output
   (heat: `error < 0`; cool: `error > 0`). The unit already delivers nothing, so
   pushing further that way is pure windup. The opposite direction is *not* held:
   moving the setpoint back toward `internal` is exactly what restarts the unit,
   so that integration is useful.

   This is the target-unreachable-in-this-mode case — summer, cool mode, target
   26, room 23, `error = +3` — and it holds through a slow multi-hour approach
   because it keys off direction and achievability, not output magnitude.
5. **`unit_full_power`** — `unit_state == full_power` and the error asks for
   **more** output (heat: `error > 0`; cool: `error < 0`). The unit is flat out and
   short of capacity. Again the opposite direction is free: backing the setpoint
   off does reduce output.

   Reasons 4 and 5 are the same rule — hold when you are against a limit and
   asking to go past it — applied at the two ends of the modulating band.
6. **`command_saturated`** — `raw > effective_max` and `error > 0`, or
   `raw < effective_min` and `error < 0`. Fallback for when `internal` is
   unavailable and 4/5 cannot be evaluated.
7. **`outside_integral_band`** — optional, **default disabled** (`0`). Hold when
   `|error| > integral_band`.

   Note this is the **inverse of a classic deadband**, which is a neutral zone
   *around* the setpoint where a controller stops acting to avoid hunting. Here the
   integral moves only when *close* to target and freezes when far — conditional
   integration, sometimes called integral separation — because the integral's job
   is to trim a residual bias, not to drive a transient. P does that. The parameter
   is named `integral_band` rather than `deadband` for exactly this reason.
8. **`integral_clamped`** — the integral is already at `integral_max` and the error
   is positive, or at `integral_min` and the error is negative. `ki` and `dt` are
   both positive, so the sign of the step is the sign of the error, which makes
   this the one limit that does not depend on the mode.

   Reasons 4, 5, 6 and 8 are the same rule on four different limits: hold when you
   are against a bound and asking to go past it. The clamp is still applied to the
   arithmetic as a backstop, but *silently* — reaching the bound is integration
   that happened, and it is only the following cycle, still pushing that way, that
   reports a hold. So `integrating` means exactly one thing: the integral did not
   move this cycle.

When `internal` is unavailable, reasons 4 and 5 are skipped and 6 and 8 carries the
load.

### 5.3 Target-change hold

On a change to `target_temperature`, integration is held for

```
duration = min(integral_hold_base + integral_hold_per_degree * |delta|,
               integral_hold_max)
hold_until = now + duration
```

Defaults: `base` 0, `per_degree` 45 min, `max` 2 h.

**`delta` is measured from the hold anchor, not from the previous target.** The
anchor is the target that was in effect when the current hold began; if no hold is
active, the change being processed sets it to the outgoing target. This makes a
UI slider dragged in 0.5 °C steps behave like the single change it is: 21 → 21.5 →
22 → 22.5 → 23 in a few seconds anchors at 21, so the final `delta` is 2.0 and the
hold is 90 minutes, not the 22.5 minutes that per-change measurement would give.

The clock restarts on each change while the magnitude accumulates, so nudging up
another degree half an hour later extends the hold rather than letting it lapse.
Returning to the anchor cancels the hold outright — `delta` of 0 means there is
nothing left to protect against.

**Strictly per mode.** Heat and cool hold independent targets (§2), so a change to
one says nothing about the other. Arming both would anchor the idle mode to a
target that is not even its own — change the cooling target from 21.5 to 22 and
heating would anchor at 21.5, so a later move of the heating target from 18.5 to
19 would compute `delta` as 2.5 rather than 0.5, and hold for two hours instead of
twenty-two minutes.

The hold is armed against the mode that owns the target: the active one, or the
last active one while off.

### 5.3.1 Everything that ends a hold

`hold_remaining` reads 0 in exactly these cases:

1. **Early release** — the first cycle where `|error| <= integral_hold_release_band`
   (default 0.3 °C). Jumps straight to 0 rather than counting down, so the timer is
   a ceiling rather than a fixed sentence: full protection on a slow approach,
   automatic release on a fast one. This is what makes the generous per-degree
   scaling safe, and unlike `integral_band` it cannot misfire when the unit is
   working but underpowered and parked short of target. A small nudge toward the
   current room temperature is often already inside the band, so it can release on
   the very next cycle.
2. **Expiry** — `now` passes `hold_until`. Counts down to 0 normally.
3. **Return to the anchor** — a change whose `delta` from the anchor is 0.
4. **`clear_hold`** — the service or the device button.
5. **Restart with nothing stored** — a hold armed before the entity ever ran a
   cycle is restored, but one that was never persisted comes back as 0.

Not a reset, but looks like one: while control is paused (§10) or the entity is
off, `step()` does not run, so `hold_remaining` freezes at its last value rather
than counting down.

Set `per_degree: 0` and `base: '01:00:00'` for a flat one-hour hold instead.

### 5.4 Overheat protection

Per mode, VTherm's mechanism: when `error` changes sign between cycles, halve the
integral. Independent of the holds above.

### 5.5 Never reset on target change

The integral carries learned bias and load, neither of which changed because the
target moved. Only §8's service and `overheat_protection` reduce it.

## 6. Write policy

The loop computes a setpoint every `sampling_period` but writes far less often: a
rate limit keeps it from chattering at the unit, and the resync compares before
writing so a steady state produces no traffic at all.

### 6.1 When a setpoint is written

A write happens when the rounded setpoint differs from the last one written **and**
either `min_setpoint_interval` (default 10 min) has elapsed, or the change came
from the user setting `target_temperature`.

A user target change skips the rate limit — that limit exists to stop the loop
chattering, not to make the UI feel unresponsive — but still sends nothing when the
rounded command has not actually moved.

### 6.2 Resync

While in `heat` or `cool`, changes made to the underlying entity elsewhere (its own
remote, another automation) are **ignored as input** and **overwritten**. At most
every `resync_interval` (default 10 min) the loop compares the underlying entity's
reported `hvac_mode`, `temperature`, `fan_mode` and high-power switch against what
it intends, and writes only the attributes that differ. When everything matches,
nothing is written at all.

### 6.3 8 °C mode — extended heating range

Some units have a frost-protection mode, often labelled "8 °C" or "eco", exposed by
their integration as a separate switch. It changes which setpoints the unit will
accept: with the switch **off** it takes the normal range (typically 17–30 °C), and
with it **on** it takes a lower one (in my case 5–16 °C). In effect the switch
extends the heating range downward.

That matters for a PID, because a computed setpoint below the normal floor would
otherwise just be clamped at 17 and leave the room running warm. Given the switch
entity, this integration uses the whole extended range and flips the switch as
needed.

Configure `eight_deg_switch` with the switch entity, `eight_deg_threshold` with the
boundary between the two ranges (default 17), and `command_min` with the true floor
(5 rather than 17). Then:

| Mode | Setpoint range | Switch |
|---|---|---|
| No `eight_deg_switch` configured | `[command_min, command_max]` | untouched |
| Heat | `[command_min, command_max]` | **on** when `command < eight_deg_threshold`, else **off** |
| Cool | `[max(command_min, eight_deg_threshold), command_max]` | **off** |

**Write sequence.** Flipping the switch makes the unit jump to a default
temperature of its own choosing, so on a transition the switch is written first,
then `eight_deg_settle` (default 5 s) is allowed for it to reach the unit, and only
then is the setpoint written. Without a transition the setpoint is written
directly. The switch is only ever touched as part of a write that was already going
ahead — a transition can only be caused by the computed setpoint crossing the
threshold — so no rate-limit bypass is needed.

### 6.4 Auto-off (optional, per mode, default off)

Enabled with `auto_off: true`. When the **raw** command leaves the writable range in
the direction meaning "less output than the unit's gentlest setting" —
`raw < command_min` in heat, `raw > command_max` in cool — the unit is stopped with
`hvac_mode: off` rather than parked at a setpoint it will overshoot. This is
evaluated on `raw` because the rounded, clamped command can never leave the range by
definition.

The loop keeps running while the unit is stopped: our entity stays in `heat`/`cool`,
only the underlying unit is off. So the command is still computed every cycle and
the unit **restarts** as soon as it comes back into range, writing `hvac_mode` and
then immediately rewriting the setpoint, since a stopped unit cannot be relied on to
have kept it.

`auto_off_margin` (default 0.5 °C) is one-sided hysteresis on the way back: the
command must climb `margin` past the boundary before restarting, so a command
sitting on the edge cannot flap the compressor. It applies to `raw`, so the room
movement it corresponds to is `margin / kp`.

Both transitions bypass `min_setpoint_interval` and the resync timer — stopping and
starting a compressor should not wait ten minutes. The state survives a restart, so
a stopped unit is not briefly started again across a reboot, and it is reported as
the `auto_off` attribute and as `hvac_action: idle`.

The integral needs no special handling: `raw` outside the range in that direction is
exactly §5.2's `command_saturated`, which already holds it.

**Where this actually fires.** Cooling is the useful case — an unreachable summer
target drives `raw` well past `command_max`, and stopping is the right answer rather
than sitting at 30. In heating it only fires where `command_min` is a real floor;
with 8 °C mode configured `command_min` is 5, so `raw` will essentially never fall
below it and `auto_off` is inert.

### 6.5 When off

Turning our entity off sends `hvac_mode: off` to the underlying entity once. While
off — and in `fan_only` — the underlying entity's state is ignored entirely and
nothing is written, so turning the AC on by hand is left alone.

## 7. Persistence

Restored across a restart: `hvac_mode`, `fan_mode`, `preset_mode`, the **per-mode
targets** and which mode last owned one, and per mode the `integral` and any
outstanding target-change hold. Gains come from YAML and so are inherently
preserved.

Sample timestamps are deliberately **not** restored, so the first cycle after a
restart cannot integrate across the downtime.

## 8. Services

| Service | Fields | Effect |
|---|---|---|
| `pid_climate.reload` | — | Re-read the YAML blocks and apply changes, no restart |
| `pid_climate.set_integral` | `value`, `mode` (default: active) | Force the integral, clamped to the mode's range |
| `pid_climate.reset_integral` | `mode` (default: all) | Zero it |
| `pid_climate.clear_hold` | `mode` (default: all) | Drop an outstanding target-change hold |

Two **button** entities on each device run the last two against the active mode
(or against every mode while the entity is off): *Reset integral* and *Clear hold*.
A third, *Force update*, has no service behind it: it runs a control cycle on the
spot (§2), so a change can be evaluated without waiting for `sampling_period`.

## 9. Configuration

```yaml
climate:
  - platform: pid_climate
    name: Living Room Regulation
    unique_id: living_room_regulation

    # I/O entities
    target_entity: climate.ac_living_room
    room_sensor: sensor.living_room_temperature
    outdoor_sensor: sensor.outdoor_temperature   # optional; without it, ke has no effect

    # AC command setup
    command_min: 5
    command_max: 30
    command_step: 1
    high_power_switch: switch.ac_living_room_high_power_mode   # optional, exposed as preset "boost"
    eight_deg_switch: switch.ac_living_room_8_degc_mode        # optional; opens a separate band
                                                               # below eight_deg_threshold, within
                                                               # command_min/max. See 6.3
    eight_deg_threshold: 17
    eight_deg_settle: '00:00:05'   # time for the switch to reach the unit before writing a setpoint
    min_setpoint_interval: '00:10:00'   # accepted command update frequency
    resync_interval: '00:10:00'         # how often the underlying entity is compared and corrected

    # Thermostat setup
    min_temp: 15
    max_temp: 28
    target_temp: 18.5              # initial value, if nothing is restored
    target_temp_step: 0.5
    fan_only: true                 # default; false hides the mode on units without it
    fan_modes: [Auto, Low, Medium, High]   # fallback until the underlying entity loads

    # PI weights setup
    sampling_period: '00:03:00'
    max_sample_gap: '00:15:00'     # a longer gap between samples does not integrate
    heat:
      offset: -2.5          # means command 16 holds 18.5 on average
      kp: 2.5               # a 0.4 deg error leads to +1 on the command
      ki: 2.0               # a 0.5 deg error over an hour progressively leads to +1 on the command
      ke: 0.38              # base command increased by 1 every 2.6 degrees outside compared to target

      # Control the I component to avoid windup
      integral_min: -4.0    # fixed bounds for I
      integral_max: 4.0     # fixed bounds for I
      ac_modulation_lower_margin: -1   # idle below internal-1: reducing the command further does nothing
      ac_modulation_upper_margin: 3    # flat out above internal+3: raising it further does nothing
      overheat_protection: false  # inspired by VTherm: halve I when the error changes sign.
                                  # Might lose a useful bias though
      integral_band: 2      # if we are further than [-2, +2] from target, don't integrate,
                            # let it get closer by itself
      integral_hold_base: '00:00:00'        # fixed part of the post-target-change hold
      integral_hold_per_degree: '00:45:00'  # per degree of target change, freeze I for up to 45 min
                                            # to let the room stabilise
      integral_hold_max: '02:00:00'         # never hold longer than this, however big the change
      integral_hold_release_band: 0.3       # reaching within 0.3 of target releases the hold early

      # Auto turn-off when not useful
      auto_off: false
      auto_off_margin: 0.5   # only with auto_off: true. Hysteresis on raw before restarting,
                             # so the room movement it corresponds to is margin / kp

    cool:
      offset: 1.0
      kp: 2.0
      ki: 0.83
      ke: 0.0
      integral_min: -4.0
      integral_max: 4.0
      ac_modulation_lower_margin: -1
      ac_modulation_upper_margin: 3
      overheat_protection: true
      integral_band: 0      # 0 disables it
      integral_hold_per_degree: '00:45:00'
      integral_hold_max: '02:00:00'
```

At least one of `heat` / `cool` is required; everything else above has a default.

`ki` is expressed **per hour**: `ki: 2.0` means 1 °C of error sustained for 30
minutes adds 1 °C of command.

`command_min` is the unit's own floor — set it to the bottom of the extended range
where 8 °C mode exists, and to the normal floor where it does not. See §6.3.

`min_temp` is the lowest **room target** the loop will accept, which is not the same
thing as `command_min`. Frost protection is a manual job on the unit, not something
to regulate.

`ac_modulation_lower_margin` and `ac_modulation_upper_margin` are integers.

## 10. Unavailable inputs

Nothing here fails loudly: the AC simply keeps whatever setpoint it last received,
and control resumes when the input comes back. The `control_blocked` attribute
names the missing input, and a warning is logged once on the way in and out.

| Input | Missing at startup | Lost while running |
|---|---|---|
| **Room sensor** | No control until it appears; a cycle is kicked off the moment it does, rather than waiting a full sampling period | Reading is **dropped**, control stops. This is the feedback signal — regulating on a frozen reading is worse than not regulating, because the loop would drive the AC from a temperature that stopped being true an hour ago |
| **Underlying AC** | No control; retried every sampling period, so an integration that loads late self-heals | Control stops, integration timing forgotten so the gap is never integrated across |
| **Outdoor sensor** | `ke` term is 0 until it appears | **Last value is kept.** Deliberately unlike the room sensor: outdoor temperature moves slowly, so a stale reading is a far better feedforward than collapsing `ke * (room - outdoor)` to zero, which would silently drop several degrees of command and leave the integral to rediscover it |
| **AC's `current_temperature`** | `unit_state` is `unknown`; §5.2's reasons 4 and 5 are skipped and `command_saturated` carries the gating | Same |
| **8 °C switch** | Setpoint write is **skipped** for that cycle and retried. The unit only accepts 5–16 with the mode on and 17–30 with it off, so writing against the wrong state gets rejected | Same |
| **High power switch** | Preset assertion skipped on resync; `preset_mode` still reports our own intent | Same |

In every case integration timing is forgotten, so the first cycle after recovery
reports `no_time_delta` and cannot integrate across the outage.

## 11. Non-goals

Autotune, PWM/switch output, window/presence/motion, power management, central
boiler, multiple units per room, valve control, auto mode switching, temperature
presets, and an *interactive* config flow — the import flow in §2 exists only to
own a device, and adds no UI configuration.

## 12. Implementation notes

For contributors; none of this is needed to use the integration.

**Why there is a config flow at all.** Home Assistant devices belong to config
entries: `entity_platform._async_add_entity` skips `device_info` entirely when
`self.config_entry` is None, which a bare YAML platform always is. So each
`climate:` block is pushed through a `SOURCE_IMPORT` flow and the entities are
created from the resulting entry. The flow has no user step — YAML stays the only
place settings live.

**Durations in entry data.** Config entries must be JSON-serialisable and
`cv.time_period` yields `timedelta`, so every duration is converted to seconds on
the way in and back to `timedelta` in the entity.

**Import replaces, never merges.** `_abort_if_unique_id_configured(updates=...)`
merges into existing entry data, which would mean a key *deleted* from YAML
silently stayed in effect — dropping `eight_deg_switch` from a block would leave it
working. The import step therefore replaces the entry's data wholesale.

**Platform setup order.** The climate platform is forwarded and awaited on its own
before the sensor and button platforms, because those read the controller object
that the climate platform publishes into `hass.data`.
