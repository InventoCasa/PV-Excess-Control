# Battery Management

PV Excess Control coordinates hybrid batteries with solar generation, household
loads and electricity prices. Automatic grid charging is off by default. Adding
control mappings or installing an update does not enable it.

## Solar strategy and target

Three strategies determine battery priority relative to managed appliances:

- **Battery First:** below the solar target, reserve charging power before
  ordinary appliance allocation. Loads may use surplus beyond the available
  battery charging limit. With no known limit, only measured charging is
  protected. Overrides and existing safety rules retain their precedence.
- **Appliance First:** allocate solar excess to appliances before the battery.
- **Balanced:** share solar excess between battery charging and appliances.

**Solar-charge target SoC** (`battery_target_soc`) and **Target Time** guide solar
planning. They remain separate from the maximum grid-charge SoC. A 60% grid
ceiling therefore still permits solar to fill a battery whose solar/native
maximum is 100%. The integration does not lower the inverter's native maximum
SoC to enforce the grid ceiling. See also [Battery First power
reservation](../energy-policy.md#battery-first-without-a-forecast) and
[dynamic solar charging](../dynamic-battery-charging.md).

## Forecast-aware grid charging

Enable **Allow Grid Charging** and **Enable forecast-aware grid charging** to
permit automatic purchase of battery energy. Configure a positive grid-charge
power and the inverter's start, stop, adjustable power and, where needed, mode
commands. Automatic planning requires an adjustable charging power entity;
manual-only operation can still use a single start/stop switch.

Set **Native maximum battery charging power** (`battery_max_charge_power_w`) to
the verified physical limit for normal solar/battery charging. This is separate
from the desired grid-charge power: a 2500 W grid setting must not make the
planner assume that native solar charging is also limited to 2500 W. The native
limit is required even when dynamic solar charging is disabled. Entering it does
not enable that separate control loop. Use physical device limits and readbacks;
an input helper's slider range or its local value may not represent the
inverter's actual charging capability.

Set the **Battery Charge Price Threshold** as the maximum permitted purchase
price, using complete gross prices in €/kWh. Its default remains 0 €/kWh; no
20 ct/kWh or seasonal target is enabled automatically.

The planner compares future schedules with normal self-consumption over the
shared, continuously covered forecast and price horizon, up to 24 hours. It
accounts for household demand, PV production, current storage, reserve, charging
and discharging power, later prices and conversion losses. It can wait for a
later cheaper slot. It only buys energy that is expected to supply household
demand within that horizon, while preserving the solar uptake expected without
grid charging. Export arbitrage and filling unused storage at the horizon are
not objectives.

The first solar charging interval is not a requirement to reach an empty
battery. A small solar surplus may leave useful storage capacity available while
retained energy avoids more expensive purchases later. The objective is lower
total modeled cost with preserved solar uptake; the integration does not force
discharge or export simply to reach zero before each charging interval.

**Maximum grid-charge SoC** (`battery_grid_target_soc`, default 80%) is a ceiling,
not an instruction to fill to that value. The computed slot target can be lower.
A sunny forecast may therefore result in little or no overnight charging. The
solar target time is not a requirement to buy enough grid energy to fill the
battery; grid purchases follow expected demand and economical time windows.

A bounded numerical search produces an approximate schedule. It retains the
original and finer storage-state searches, then additionally tries smaller
charging quantities when physically verified discharge hold is available. Each
extra search has its own fixed computation budget. Only a completed candidate
with better modeled economics can replace an already completed plan. Without
verified hold, the additional smaller-quantity search is omitted. All candidates
must still honor actuator power steps, solar uptake and useful future demand.
This improves selection without promising a global optimum or realized savings.

The pure search runs outside Home Assistant's event loop. Its detached input
snapshot is revalidated before a result can drive a command, including current
SoC, tariff and configured power/price limits. Stop, unload and watchdog requests
interrupt waiting for a search; an abandoned computation cannot publish a plan.

Load metadata that leaves the actual demand, solar availability and discharge
limit unchanged does not create extra planning boundaries. Tariff, forecast,
local-hour, genuine load/cap and restart-eligibility boundaries remain intact.

Configured actuator minimum, maximum and power steps apply to the combined
PV-plus-grid forced-charge setpoint before its energy and cost are scored.
Unusable residual purchases are excluded rather than counted as free holding.
Native solar charging keeps its separate physical limit. These numeric limits
do not measure efficiency at low charging power. A reached SoC target or a
safety condition can still stop charging before the forecast interval ends.

 When cost and
total purchased energy are otherwise equal, it prefers earlier useful charging
to avoid arbitrary shifts between equally priced windows. Lower prices and
solar headroom retain their priority. Forecasts and household demand remain
estimates; projected savings are not measured savings.

### Calibrating the PV forecast

**PV forecast factor for grid charging** (`battery_pv_forecast_factor`) scales
expected PV energy only for battery grid-charge planning. The default **1.0**
uses the full forecast; **0.9** uses 90%. The supported range is 0.1–1.0.
Reducing the factor can increase the additional grid energy planned when useful
and economical. It does not change the provider's sensors, solar target,
household demand profile or independent solar/appliance planning.

Choose the factor from representative historical forecasts available at the
time the charging decision would have been made, compared with actual
production over the same intervals. A forecast updated after sunrise cannot
reconstruct the uncertainty of an earlier overnight decision. Seasonal weather
and shading may change the bias, so review the factor as conditions change and
avoid applying the same correction twice if the provider already calibrates
its estimates. A lower forecast remains an assumption, not a guarantee of
production. Missing, invalid or stale forecast inputs still block charging.

### Consumers and available solar energy

A forecast of PV production is not automatically a forecast of battery charging.
The battery planner accounts for current consumers before assigning solar energy
to storage. Mapped EV charging is tracked separately because it is excluded from
the learned household profile. Other household loads raise a **total demand
floor** when observed consumption exceeds the historical estimate; the planner
uses the larger demand rather than adding the same appliance twice.

Currently running consumers with a defensible operating limit can occupy solar
energy through that limit. Examples include the end of their continuous eligible
cheap-price window or a configured remaining-runtime/maximum-runtime limit.
Minimum daily runtime, an advisory plan or an advisory planning deadline alone
do not establish a switch-off time. A cheap window provides a stopping estimate
only when the configured policy and available solar do not permit continuation. Starting, stopping or changing a consumer's policy causes the
battery schedule to be reconsidered.

Daily-runtime limits are anchored to the observation of the consumed runtime.
Rechecking the same observation after a calculation must not move its expected
end time. New runtime observations, operating changes and the local daily reset
are revalidated before the plan can drive a command.

Unmapped non-EV household background is a near-term estimate. Its trailing
two-minute, time-weighted mean initially contributes for at most two minutes;
the current excess contributes for at most one minute from its actual sensor
report. Sustained observations can extend the mean's projection to five, ten
or thirty minutes only after the corresponding duration has been observed.
A short kitchen load therefore does not immediately become a thirty-minute
increase in expected demand. Regularly cycling loads retain their average energy.

During warm-up, the household profile fills unobserved time. Duplicate reports
do not add weight or renew an expired horizon; missing readings, changed meter
mappings or observation gaps longer than two minutes reset the estimate. The
mean can remain above the current load for up to two minutes after a spike ends.
The background added to known household runs also loses transient increases when
this short horizon ends. Its established reference may rise only after thirty
minutes of sustained observations and remains capped by the future hourly
profile. Known metered runs retain their separate duration bounds.

Known metered household runs and bounded EV demand remain immediate. The same
background estimate accompanies known household runs without counting their
metered power twice. Actual consumption learning and safety limits use current
measurements, not the mean. No unknown load is extrapolated unchanged for 24
hours. An active EV excluded from household history needs valid power and a
credible duration bound; if these are unavailable, automatic purchases are
blocked with a diagnostic reason instead of assuming its solar demand is zero.

The calculation also respects the discharge limit expected while those loads
run. A device whose minimum setting is 100 W is modeled with that remaining
power, not as a complete discharge hold. The integration's existing appliance
safety rules still determine actual operation; the battery forecast does not
create new appliance start commands.

### Costs and losses

The useful-energy cost is:

```text
purchase price / round-trip efficiency + wear allowance
```

The default **round-trip efficiency is 0.85**, a planning assumption rather than
a measurement of a particular battery. A more cautious comparison can use 0.80.
The optional wear allowance defaults to **0 €/kWh**. For example, buying at
0.20 €/kWh with 0.85 efficiency and an assumed 0.03 €/kWh wear allowance costs
about 0.265 €/kWh of later usable energy. Charging is useful only when it avoids
more expensive grid purchases after accounting for forecast PV and demand.

### Required inputs

Automatic charging requires:

- Valid battery capacity, SoC, adjustable grid-charge power and a separate,
  positive native charging limit, with targets at or above the configured
  reserve.
- Fresh SoC, household load and PV power readings. The default maximum age is
  300 seconds, configurable from 30 to 3600 seconds. Missing, unavailable,
  non-finite or stale readings cannot start automatic charging and cause an
  owned charge/hold to be released.
- Future tariff windows and solar forecast intervals with continuous coverage
  throughout the planning horizon. Gaps and overlaps invalidate the plan;
  missing prices or PV values are not replaced by zero. The current price must
  match its tariff window. Forecast freshness defaults to six hours.
- A household demand profile for all 24 local hours.
- Physical acknowledgement of automatic inverter commands.

The household profile can be entered as **Hourly household demand**, a JSON list
of 24 nonnegative average watt values, ordered from local hour 00 to 23. Exclude
mapped EV charging, but include the usual non-EV household consumers. A constant 500 W example is:

```json
[500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500,
 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500]
```

Leave the field empty to learn demand from household load measurements. Mapped
EV demand is subtracted when its power reading is available. Learning needs at
least **30 minutes of valid samples for every one of the 24 local hours**,
usually at least one day; missing readings delay readiness. Each hour must have
recent observations within seven days. The learned profile is persisted across
reloads and midnight. Clearing the explicit profile returns to learning.

## Restart eligibility

**Grid-charge restart delay** (`grid_charge_engage_min_duration_minutes`, default
five minutes) is an off-time after a confirmed owned grid-charge stop. Every
release path, including watchdog expiry and changed inputs, starts the same
pause after successful confirmation. The deadline survives reload/restart and
is visible as `battery_restart_available_at`; `restart_delay` explains waiting.
Repeated inactive cleanup and hold-only releases do not renew this charge pause.

The plan includes the current restart deadline and applies the same delay after
future planned stops. A running charge may continue without being treated as a
new start. If a future deadline falls inside an otherwise indivisible planning
interval, the next available interval is used; the schedule remains an
approximation. Disabling control, unloading or losing required data still stops
an owned charge immediately. A restart delay does not impose a minimum on-time.

## Inverter acknowledgement and shutdown

Start, stop, mode and power writes run in sequence with bounded waits. The
integration checks the reported command state and power setpoint. A physical
readback of the power **setpoint** is distinct from instantaneous battery power,
which can vary because of battery or inverter limits.

An `input_select`, `input_boolean` or `input_number` helper confirms a local
request only. Automatic charging requires a separate physical feedback entity
for every such mapped helper. Examples include an inverter's mode register,
charge command register and forced-charge power register. Direct `select`,
`switch` or `number` entities may provide their own physical feedback; verify
that the device integration actually reports the device state. Manual charging through input helpers also waits for configured physical
feedback. Incomplete legacy helper mappings are shown as helper-only confirmation
and cannot start forced charging.

Turning **Control Enabled** off, unloading the integration, losing required
inputs or encountering an unconfirmed command releases control started by this
integration. Stop and the configured return-to-self-consumption mode are both
attempted after a partial failure. Ownership and pending cleanup persist so a
restart can recover unfinished release. Shutdown requests release before Home
Assistant stops its automation and hardware services. Restored ownership is
recovered after startup completes so helper-based command bridges can process
the request. New automatic charging waits while cleanup is unresolved. If
communication is unavailable, software cannot confirm that the inverter stopped;
pending cleanup remains visible and is retried.

## Holding energy for expensive hours

A hold prevents cheap stored energy from being discharged immediately while
import prices are still low. It is used only when **Enable physically verified
discharge hold** is explicitly selected and a complete control mapping exists.
Configure hold/release command values and feedback, with separate expected
feedback values when the inverter reports different text from the command.
Helper controls require a distinct physical feedback entity.

Exactly adjacent hold intervals in a valid plan share one hold deadline, avoiding
a release at their common boundary. A gap, a different planned action, invalid
inputs or an explicit stop still ends holding; sensor freshness continues to
limit the watchdog deadline. A changed plan is revalidated after hardware waits.

Numeric `number` and `input_number` controls are also supported. Enter command
values in the control entity's native units and, when needed, separate numeric
feedback values in the feedback entity's units. For example, a kW control and a
W sensor need explicit corresponding values. Numeric feedback must match the
setpoint closely; the wider charging-power tolerance cannot confirm a hold.
Invalid, non-finite, negative or identical engage/release values are rejected.

For a numeric helper whose device writer listens only to state changes, configure
an optional **hold command bridge automation**. If a previous write was not
physically confirmed and the helper already shows the desired value, the
controller can explicitly trigger that automation again. It respects the
configured automation conditions and never pulses a different setpoint to force
an event. Select only the existing automation that writes this helper's current
value to the inverter. This mapping is retained with cleanup ownership so retries
still work after reload or options changes.


Some Sungrow installations can stop battery discharge using a **10 W** maximum
discharge setpoint while retaining self-consumption mode; the
[mkaiser configuration](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant/blob/310635c6e2c1122f5adb54fba7fdf33a7cb85142/modbus_sungrow.yaml#L2364-L2368)
provides such a scene. This does not establish that a zero register value is
supported. Check the protocol, installed helper limits and measured battery
power/current on the actual inverter. Confirm normal supply resumes after
release and that PV charging remains available. Changing a helper's minimum
alone is not proof of a working hold.

When the hold uses the same numeric discharge channel as appliance protection,
a single serialized writer combines both requests. Holding retains priority;
normal release applies the current consumer limit directly instead of briefly
restoring unrestricted discharge. Every owned restriction on that channel has a
persisted release mapping, including consumer restrictions. Disabling control,
unloading or recovering after restart restores the recorded fallback; an
unconfirmed release stays pending and is retried.


Verify both that the command prevents discharge and that release restores
self-consumption before opting in. A maximum discharge setting with a minimum
of **100 W does not provide a complete hold**. A control name such as “Stop”
does not establish its physical effect in every inverter mode.

Without verified hold, the planner assumes normal self-consumption between
charging windows. It does not assume it can reserve energy for an arbitrarily
later expensive period. It can still choose useful charging when savings remain
under that limitation. Discharging uses the inverter's normal self-consumption
mode; the integration does not force battery export.

Charge, hold and self-consumption are explicit control states. A default
**2 percentage-point SoC hysteresis** and the configured minimum interval reduce
repeated small charging starts. Shutdown and invalid-input handling take
precedence over timing preferences.

## Existing discharge protection

High-power appliances marked **Big Consumer** can request a per-appliance
**Battery Discharge Override**. The **Minimum Battery SoC** setting can shed
non-essential appliances and limit discharge when storage is low. These policies
retain their existing safety precedence. The inverter's native reserve is an
independent hardware setting; configuring a planning reserve does not change it.
Set the planning reserve consistently with the native reserve.
If the configured SoC sensor already reports only usable storage above the
native reserve, its 0% means that protected reserve has been reached. Use capacity
and planning-reserve values that match that sensor's scale.

## Grid-charge notifications

Start messages require an owned charging command and sustained evidence of an
actual grid contribution to battery charging. The displayed power is the grid
contribution estimated from current readings. A configured signed grid meter
supplies import evidence; export-only installations use household, PV and battery
power balance, with fresh export evidence excluding grid charging. Unknown or
stale required inputs cannot confirm a start. The primary positive charging
reading must be at most 60 seconds old; other required readings follow the
configured battery-input freshness limit.

A start requires more than 100 W for at least 30 seconds. Once announced, a grid
contribution of at most 50 W for 60 seconds ends the episode. A confirmed release
of owned charging can close the episode immediately. These observation rules
filter short fluctuations without changing inverter control or the charging plan.
A stop is sent only for an announced start; its delivery is not suppressed by the
start-message rate limit. Notification failures do not change battery control.

## Status and troubleshooting

The integration's status sensor includes:

| Attribute | Meaning |
|---|---|
| `battery_control_state`, `battery_control_reason` | Current action or reason control is blocked/releasing |
| `battery_grid_charge_observed_power_w` | Current grid contribution estimated from fresh power readings; `null` when evidence is unavailable |
| `battery_confirmation` | Physical, helper-only or unconfigured acknowledgement |
| `battery_charge_cleanup_pending`, `battery_hold_cleanup_pending`, `battery_discharge_cleanup_pending` | Release still needs confirmation |
| `battery_hold_supported` | Verified hold mapping with physical acknowledgement is available |
| `battery_load_profile_ready` | All required hourly household demand values are available |
| `battery_grid_energy_kwh`, `battery_grid_target_soc` | Planned purchased energy and computed grid target |
| `battery_estimated_savings` | Forecast saving relative to normal self-consumption |
| `battery_plan` | Time slots, actions, grid energy and expected ending SoC |

If charging does not start, check the reason, data freshness, forecast/price
coverage, profile readiness, purchase-price ceiling and physical readback
mappings. A valid plan can correctly buy no energy when the price spread does
not cover losses, later solar is sufficient, or the remaining demand is small.

Automatic economic charging also requires an adjustable charge-power entity.
A single switch can still be used for manual charging, but cannot execute the
variable power requested by an economic schedule. An existing dynamic solar
charge cap is released before grid charging or verified holding; independent
native and large-consumer discharge protections keep their precedence and may
reduce the savings forecast by the planner.
