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

**Maximum grid-charge SoC** (`battery_grid_target_soc`, default 80%) is a ceiling,
not an instruction to fill to that value. The computed slot target can be lower.
A sunny forecast may therefore result in little or no overnight charging. The
solar target time is not a requirement to buy enough grid energy to fill the
battery; grid purchases follow expected demand and economical time windows.

A bounded numerical search produces an approximate schedule. Forecasts and
household demand remain estimates; projected savings are not measured savings.

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

Unmapped or otherwise unbounded non-EV household consumption is a near-term
estimate: measured load persists for a bounded 30-minute observation window and
is updated as new measurements arrive. It is not assumed to run unchanged for
24 hours. An active EV excluded from the household history needs valid power and
a credible duration bound; if these are unavailable, automatic purchases are
blocked with a diagnostic reason instead of assuming that its solar demand is
zero.

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
restart can recover unfinished release. New automatic charging waits while
cleanup is unresolved. If communication is unavailable, software cannot confirm
that the inverter stopped; pending cleanup remains visible and is retried.

## Holding energy for expensive hours

A hold prevents cheap stored energy from being discharged immediately while
import prices are still low. It is used only when **Enable physically verified
discharge hold** is explicitly selected and a complete control mapping exists.
Configure hold/release command values and feedback, with separate expected
feedback values when the inverter reports different text from the command.
Helper controls require a distinct physical feedback entity.

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

## Status and troubleshooting

The integration's status sensor includes:

| Attribute | Meaning |
|---|---|
| `battery_control_state`, `battery_control_reason` | Current action or reason control is blocked/releasing |
| `battery_confirmation` | Physical, helper-only or unconfigured acknowledgement |
| `battery_charge_cleanup_pending`, `battery_hold_cleanup_pending` | Release still needs confirmation |
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
