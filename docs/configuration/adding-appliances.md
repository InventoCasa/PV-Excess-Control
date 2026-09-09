# Adding Appliances

Each appliance is a sub-entry of the PV Excess Control integration. Add as many as you need.

## How to Add an Appliance

1. Go to **Settings → Devices & Services → PV Excess Control**
2. Click the **Add sub-entry** button on the integration card
3. Fill in the fields described below
4. Click **Submit**

The appliance will appear as a device in Home Assistant with its own switch, sensor, and number entities.

---

## Core Fields

| Field | Description |
|-------|-------------|
| **Name** | Friendly name (e.g. "EV Charger", "Heat Pump") |
| **Switch Entity** | The `switch` or `input_boolean` that turns the appliance on/off |
| **Priority** | 1 = highest priority, 1000 = lowest. Lower priority appliances turn on first when excess is available and shed first when excess drops. |
| **Nominal Power** | Expected wattage when running (used for planning and excess calculation) |
| **Actual Power Sensor** | Optional: a power sensor for real-time consumption tracking (W or kW — automatically converted) |
| **Phases** | 1 or 3 — used to convert amps to watts for dynamic current |

---

## Dynamic Current (EV Chargers / Wallboxes)

Enable **Dynamic Current** for appliances that accept variable amperage:

| Field | Description |
|-------|-------------|
| **Current Entity** | The `number` entity that controls charging amps |
| **Min Current** | Minimum amps to use (e.g. 6 A — EVSE minimum) |
| **Max Current** | Maximum amps to use (e.g. 16 A or 32 A) |

The optimizer will adjust current up and down every cycle to precisely consume available excess rather than switching on/off.

---

## EV-Specific Fields

| Field | Description |
|-------|-------------|
| **EV SoC Sensor** | A sensor showing the EV's battery percentage (0-100) |
| **EV Connected Sensor** | A binary sensor -- True when the car is plugged in |
| **EV Target SoC** | Target battery percentage (e.g., 80). Charging stops when the EV's SoC reaches or exceeds this value. |

When the EV is not connected, the appliance is automatically skipped. When SoC reaches the configured target, charging stops.

---

## Runtime Constraints

| Field | Description |
|-------|-------------|
| **Min Daily Runtime** | Minimum minutes per day the appliance must run. This is a hard constraint -- appliances with unmet min_runtime are protected from shedding. |
| **Max Daily Runtime** | Maximum minutes per day -- appliance is forced off after this |
| **Max Daily Activations** | Maximum number of times per day the appliance may be turned on. Leave empty for unlimited. Useful for appliances with limited cycle life (e.g., compressors) or appliances that should not restart frequently. |
| **Schedule Deadline** | Time by which min runtime must be completed (e.g. `07:00`) |

The planner uses these constraints to schedule appliances in advance, using cheap tariff windows or forecast solar to meet deadlines.

---

## Time Window Constraints

| Field | Description |
|-------|-------------|
| **Start After** | Earliest time of day the appliance is allowed to turn on (e.g., `08:00`) |
| **End Before** | Latest time of day the appliance is allowed to run (e.g., `20:00`) |

Outside of the configured time window, the optimizer will not turn the appliance on. This is useful for appliances that should only run during daytime hours (e.g., a pool pump) or to avoid running noisy appliances at night.

---

## Appliance Dependencies

| Field | Description |
|-------|-------------|
| **Requires Appliance** | Another appliance that must be running before this one can start |

Use this to chain appliances. For example, a heat pump circulation pump that should only run when the heat pump itself is running.

---

## Averaging Window

| Field | Description |
|-------|-------------|
| **Averaging Window** | Custom history window in seconds for excess power averaging (overrides the global default) |

By default, the optimizer averages excess power over a recent history window to smooth sensor noise. Set a custom averaging window per appliance if you need a longer or shorter smoothing period (e.g., a shorter window for fast-reacting appliances like EV chargers).

---

## Big Consumer Protection

Mark an appliance as **Big Consumer** (e.g. a heat pump or tumble dryer) to enable battery discharge protection. When these appliances are running, the integration can limit the battery discharge rate to prevent excessive grid supplementation.

Set **Battery Discharge Override** to the maximum watts the battery should discharge while this appliance is on.

---

## Advanced Fields

| Field | Default | Description |
|-------|---------|-------------|
| **On Only** | Off | Once turned on, never turn off (good for dishwashers mid-cycle) |
| **Protect From Preemption** | Off | When enabled, this appliance will never be shed to make room for a higher-priority appliance (requires global preemption to be enabled) |
| **Switch Interval** | 300 s | Minimum seconds between state changes |
| **Allow Grid Supplement** | Off | Allow a small grid draw to keep the appliance running |
| **Max Grid Power** | -- | Maximum imported watts, in addition to solar, when tariff support is eligible. Applies to both fixed and dynamic loads |

> **Note:** Battery-level protection is configured in the battery settings, not per appliance. The `min_battery_soc` threshold (set during initial setup or in options) causes the optimizer to shed appliances when the battery SoC drops below the configured minimum.

---

## Example Configurations

### EV Charger

```
Name: EV Charger
Switch Entity: switch.wallbox_charging
Priority: 10
Nominal Power: 7400 W
Phases: 3
Dynamic Current: Yes
  Current Entity: number.wallbox_charging_current
  Min Current: 6 A
  Max Current: 16 A
EV SoC Sensor: sensor.my_car_battery
EV Connected Sensor: binary_sensor.wallbox_car_connected
EV Target SoC: 80
Schedule Deadline: 07:00
Min Daily Runtime: 60 min
```

### Heat Pump

```
Name: Heat Pump
Switch Entity: switch.heat_pump_heating
Priority: 20
Nominal Power: 2000 W
Phases: 1
Big Consumer: Yes
Battery Discharge Override: 500 W
On Only: Yes
Switch Interval: 600 s
```

### Dishwasher

```
Name: Dishwasher
Switch Entity: switch.dishwasher_smart_plug
Priority: 50
Nominal Power: 1800 W
Actual Power Sensor: sensor.dishwasher_power
On Only: Yes
Min Daily Runtime: 60 min
```

### Pool Pump

```
Name: Pool Pump
Switch Entity: switch.pool_pump
Priority: 100
Nominal Power: 800 W
Max Daily Runtime: 240 min
Switch Interval: 300 s
```

---

## Common Issues

**Appliance switches on but nothing happens**
Verify the switch entity works in Developer Tools. Some smart plugs expose `switch.X` but the device needs a different service call — check the switch entity in HA.

**Appliance switches too frequently**
Increase the **Switch Interval** to at least 300–600 seconds for devices sensitive to frequent cycling (heat pumps, compressors).

**Dynamic current not adjusting**
Ensure the number entity for current is writable and that the min/max current values match your EVSE's actual limits.

See [grid support and meter availability](../energy-policy.md#stable-tariff-support) for repeated-cycle behavior and power estimates.
