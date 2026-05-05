# Sensor Mapping

The integration needs power sensors from your inverter to calculate excess solar power and make control decisions.

---

## Required vs Optional Sensors

| Sensor | Required? | Description |
|--------|-----------|-------------|
| PV Power | If no Import/Export | Power currently produced by the solar panels |
| Grid Export Power | If no Import/Export | Power being sent to the grid (positive = export) |
| Import/Export Power | Alternative | Combined grid sensor (positive = export, negative = import) |
| Load Power | Optional | Total house consumption |
| Battery SoC | Hybrid only | Battery state of charge (0-100 %) |
| Battery Power | Hybrid only | Combined battery charge/discharge power (positive = charging) |
| Battery Charge Power | Hybrid only | Battery charging power (alternative to combined Battery Power) |
| Battery Discharge Power | Hybrid only | Battery discharging power (alternative to combined Battery Power) |
| Battery Capacity | Hybrid only | Battery total capacity in kWh |

> **Units:** All power sensors can report in **W** or **kW** — the integration reads the sensor's `unit_of_measurement` attribute and converts automatically. No manual conversion needed.

You need **at least one** of: `PV Power + Load Power`, `PV Power + Grid Export`, or `Import/Export Power`.

---

## Excess Power Calculation

The integration calculates excess power depending on which sensors are configured:

**If Import/Export sensor is provided:**
```
excess = grid_export - grid_import
```
(The import/export sensor value is split into its import and export components.)

**If Grid Export sensor only:**
```
excess = grid_export
```
(Falls back to `pv_production - load_power` when export reads zero.)

**If neither (PV + Load only):**
```
excess = pv_production - load_power
```

Note: Battery power is **not** added to the excess calculation. The battery's effect on excess is already reflected in the grid import/export or load values that the inverter reports.

---

## Common Inverter Brands

### Solis / Ginlong
```yaml
PV Power:        sensor.solis_pv_power
Import/Export:   sensor.solis_grid_import_export  # positive = export
Battery SoC:     sensor.solis_battery_soc
Battery Power:   sensor.solis_battery_charge_discharge_power
```

### SMA
```yaml
PV Power:        sensor.sma_total_power
Grid Export:     sensor.sma_grid_feed_in
Battery SoC:     sensor.sma_battery_charge_status
Battery Power:   sensor.sma_battery_power
```

### Fronius
```yaml
PV Power:        sensor.fronius_pv_power
Import/Export:   sensor.fronius_meter_power  # positive = export
Battery SoC:     sensor.fronius_battery_soc
Battery Power:   sensor.fronius_battery_power
```

### Huawei SUN2000
```yaml
PV Power:        sensor.huawei_solar_power
Import/Export:   sensor.huawei_solar_grid_exported_power  # with sign
Battery SoC:     sensor.huawei_solar_battery_state_of_capacity
Battery Power:   sensor.huawei_solar_charge_discharge_power
```

### Shelly EM (no inverter integration)
```yaml
PV Power:        sensor.shelly_em_pv_channel_power
Import/Export:   sensor.shelly_em_grid_channel_power  # positive = export
```

---

## Sign Conventions

Different inverters use different sign conventions for grid power. The integration handles both:

- **Export positive**: Grid Export Power sensor, value is positive when exporting
- **Export positive, import negative**: Import/Export Power sensor (set this field instead)

> **Migrating from the blueprint?** The original PV Excess Control blueprint used the opposite convention for the combined sensor (*positive = import, negative = export*). The new integration uses **positive = export, negative = import** for Import/Export. If your excess power looks inverted after migration, either negate the sensor with the template below or use the separate **Grid Export Power** field instead.

If your sensor uses the opposite convention (export negative on the export sensor), create a template sensor to invert it:

```yaml
# configuration.yaml
template:
  - sensor:
      - name: "PV Grid Export"
        unit_of_measurement: W
        state: {{ -(states('sensor.p1_meter_power') | float(0))}}
```

---

## Common Issues

**Excess power is always zero**
Check that the sensor entities exist in Developer Tools -> States and return numeric values (not "unavailable" or "unknown").

**Excess power has wrong sign**
You may have the import and export sensors swapped, or need to invert one. Use a template sensor.

**Battery power not tracked**
For hybrid inverters, ensure Battery Power uses a consistent sign convention (positive = charging, negative = discharging is common but not universal -- check your inverter docs).

**Separate charge/discharge sensors**
If your inverter exposes separate sensors for charging and discharging power instead of a combined sensor, use the **Battery Charge Power** and **Battery Discharge Power** fields instead of the combined **Battery Power** field. Both values should be positive (W or kW).
