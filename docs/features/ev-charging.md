# EV Charging

PV Excess Control provides first-class EV charging support with dynamic current control, SoC awareness, deadline scheduling, and connection detection.

---

## Features

- **Dynamic current** -- Continuously adjusts amps to consume exactly available solar excess
- **SoC awareness** -- Stops charging when EV reaches the configured target SoC
- **Connection detection** -- Pauses charging logic when the car is not plugged in
- **Runtime deadline scheduling** -- Prioritizes a configured runtime requirement; reaching a vehicle target still depends on available energy and conditions
- **Cheap tariff fallback** -- Charges during off-peak hours if solar is insufficient
- **SOC-based tariff forecast** -- Optional capacity and efficiency estimate chronological eligible charging windows shared with home-battery planning

---

## Configuration

Add your EV charger as an appliance with these settings:

```
Name: EV Charger
Switch Entity: switch.wallbox_charging    # or input_boolean controlling the charger
Priority: 10                               # low number = high priority
Nominal Power: 7400 W                     # 32A x 1-phase x 230V or similar
Phases: 3

Dynamic Current: Yes
  Current Entity: number.wallbox_current
  Min Current: 6 A
  Max Current: 16 A

EV SoC Sensor: sensor.my_car_battery_level     # 0-100 %
EV Connected Sensor: binary_sensor.wallbox_car_connected
EV Target SoC: 80                              # stop charging at 80%

Schedule Deadline: 07:00
Min Daily Runtime: 90 min
```

---

## How SoC Is Used

The EV SoC sensor works with the **EV Target SoC** configuration field to control charging:

- **EV Target SoC** -- A user-configured percentage (e.g., 80%). When the EV's SoC reaches or exceeds this target, charging stops. The appliance is excluded from allocation in the optimizer.
- **Disconnected** -- The appliance is skipped entirely for the current cycle when the EV is not plugged in.

Configure the target SoC per appliance when adding or editing the EV charger in the integration's settings. This allows you to set different targets for different EVs if you have multiple chargers.

---

## Deadline Scheduling

When a **Schedule Deadline** is set (e.g. `07:00`), the runtime-based planner aims to supply the configured **Min Daily Runtime** before that time, subject to constraints and available conditions. This is not a guarantee that the vehicle reaches a particular SOC.

The planner:
1. Calculates how much runtime remains to meet the minimum
2. Finds the cheapest available hours (solar excess or cheap tariff) before the deadline
3. Schedules charging in those hours

This means overnight cheap-rate charging happens automatically without separate automations.

---

## SOC-Based Tariff Demand Forecast

As an alternative to a minimum or external remaining-runtime requirement,
configure **EV battery capacity sensor** (`ev_battery_capacity_entity`) or a fixed
**EV forecast capacity** (`ev_battery_capacity_kwh`). Dynamic current control,
an SOC sensor, a connection sensor and a target SOC are required. A mapped
capacity sensor accepts kWh, Wh, kJ, MJ or J and takes precedence over the fixed
value; an unavailable mapped source does not silently fall back to a value.

The estimate is:

```
remaining AC energy = capacity × max(0, target SOC − actual SOC) / 100 / EV efficiency
estimated duration = remaining AC energy / expected charging power
```

**EV charging efficiency** (`ev_charge_efficiency`) defaults to **0.90**. It is an
assumption about AC input reaching the vehicle battery, separate from the home
battery round-trip assumption. An active run uses fresh measured charging power
when available; later runs use the configured tariff-funded current, current
steps and grid-power allowance. No extra solar credit is used to exceed that
allowance in the forecast. Actual solar-following control may deliver more power.

The forecast follows eligible tariff windows **chronologically**, matching the
runtime price rule, including its existing below-feed-in-price permission. It
does not restrict charging to a generic one-hour preview or wait for a later
cheaper slot when runtime policy permits charging now. It stops at the estimated
energy target and reports unmet demand when known eligible windows are too short;
it does not authorize purchases outside the runtime tariff rule to fill a target.

The shared EV horizon uses known tariff windows up to 48 hours. The energy
requirement does not reset at local midnight; configured daily maxima do.
Ordinary appliance planning retains its existing horizon. Vehicle connection,
target, operating windows and runtime protections still govern real commands.
An estimated completion time is not a forced hardware stop or a minimum-on time.

Scope is explicitly **`grid_funded_windows`**. Additional solar-following charging
continues to react to measured surplus. Forecasts remain conditional on the
vehicle staying connected, accepting the expected power and its actual target
not already being reached. Fresh SOC must be known, between 0 and 100, and reported
within 24 hours. Missing data is unknown, not zero demand or completed charging.
External remaining-runtime requirements, protected contiguous-runtime requests,
and complex dependency schedules are not invented by this forecast; conflicting
runtime configuration is rejected or explicitly reported unavailable.

The immutable estimate refreshes at bounded five-minute intervals and when SOC,
connection, measured running power or policy changes. Time passing during an
economic calculation alone cannot continually move its end time. Re-estimation
uses observed SOC; previously planned energy is never treated as delivered.

`sensor.pv_excess_control_plan_confidence` exposes per-vehicle
`ev_demand_forecasts`: status, capacity, SOC, target, efficiency, remaining and
planned AC energy, unmet energy, estimated minutes and known horizon. Its ordinary
plan entries use the same future EV runs as the home-battery forecast.

### Solar Reservation for the Home Battery

Future EV demand is kept separate from the household profile. Measured active
load replaces only overlapping predictions for the same vehicle. A funded
active run near its SOC target is bounded by its shared completion estimate;
future runs remain present. Unknown optional EV telemetry keeps the existing
aggregate-household fallback, with explicit uncertainty for unavailable future
EV estimates rather than invented future demand.

Optional **Reserve all solar during forecast EV charging** (`ev_reserve_all_pv`)
assumes no surplus PV is available to charge the home battery during a predicted
EV run. Actual EV demand and household supply remain physically bounded; any
remaining solar excluded by this conservative assumption is accounted separately
as withheld PV, never as invented vehicle consumption. Actual running load takes
precedence over a future estimate. This setting changes planning, not inverter
registers or the configured PV forecast factor.

The battery search preserves its existing completed candidates and adds up to
20 bounded fixed-power episode comparisons with native operation outside the
episode. Each retains the same power, PV, terminal-energy, startup and restart
guards. This helps when the wider search exhausts its budget; it does not promise
a globally cheapest schedule or measured savings.

---

## Multiple EVs

Add a separate appliance for each EV charger. Assign different priorities:

```
EV Charger 1 (daily driver):  Priority 5
EV Charger 2 (weekend car):   Priority 30
```

The optimizer fills higher-priority chargers first and uses remaining excess for lower-priority ones.

---

## Integration with Common Charger Brands

### go-eCharger
```
Switch Entity:   switch.go_echarger_charging
Current Entity:  number.go_echarger_max_current
```

### Wallbox Pulsar Plus
```
Switch Entity:   switch.wallbox_charging
Current Entity:  number.wallbox_max_charging_current
Connected Sensor: binary_sensor.wallbox_status_description  # check state values
```

### OCPP Charger
```
Switch Entity:   switch.ocpp_charge_point
Current Entity:  number.ocpp_charge_limit_amps
```

---

## Common Issues

**Car charges even when disconnected**
Ensure the **EV Connected Sensor** is mapped correctly and returns `on` only when connected. Check the sensor state in Developer Tools -> States.

**Charging stops too early**
Check the actual vehicle target, connection, tariff permission and protection
reason. A forecast window is not a forced runtime. Use either the SOC-based demand
forecast or a separate runtime requirement; do not add a fictitious minimum
runtime merely to lengthen the displayed EV preview.

**Charging current too low**
If excess is consistently below the minimum current threshold (6 A x 3 phases x 230 V = 4140 W), consider enabling **Allow Grid Supplement** with a small allowance (e.g. 500 W) to bridge the gap.
