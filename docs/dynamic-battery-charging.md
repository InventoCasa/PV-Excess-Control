# Dynamic Battery Charging

PV Excess Control can throttle the battery's max charging power over the
day to (a) absorb PV that would otherwise be curtailed by the inverter's
export limit and (b) defer morning charging when the day's forecast
shows enough peak production to fill the battery on its own.

This feature controls the **solar charging power cap**. The separate
[forecast-aware grid charging](features/battery-management.md#forecast-aware-grid-charging)
feature decides whether buying energy for later household use is economical.
Solar charging keeps its own target; the grid-charge target does not lower the
inverter's native maximum SoC. A 100 W charging or discharging limit is not a
complete discharge hold. Holding energy requires its own verified control.

The solar charging cap is released when grid charging needs the inverter and
when integration control is disabled or unloaded. The grid-charge controller's
separate physical acknowledgement and recovery behavior is described in the
battery-management documentation.

## What it does

Two layers, both opt-in:

**Reactive ceiling-follower** — every coordinator cycle, when measured PV
excess is about to exceed the configured export soft ceiling, the battery's
max-charge cap is lifted to absorb the surplus.

**Forecast morning-throttle** — every planner cycle (~15 min), a 24 h
max-charge-power curve is computed from the solar forecast, residual
curtailment after appliances, current SoC, and the target SoC/time.
Mornings get the trickle value when the day's curtailment can fill the
battery without help; midday curtailment slots get the absorbed-power
setpoint; afternoons back-fill if needed.

### Concrete example

10 kWp inverter, 6 kW soft export ceiling, 10 kWh battery currently at
30 % SoC with target 80 % at 18:00. Forecast shows 4 h of curtailment
between 12:00 and 16:00 with peak excess of 8 kW.

Result: battery cap held at 100 W from 09:00 to 11:00 (battery sits at
30 %). At 12:00, cap rises to ~2 kW following the residual curtailment.
At 14:00, peak production lifts cap to ~4 kW. By 16:00, battery hits
80 % SoC. Afternoon (16:00–18:00) returns to trickle.

If forecast is wrong and the battery is still at 50 % at 16:30, a
late-day backfill kicks in: cap rises to max-power for the last hour
to still hit 80 % by 18:00.

## When to enable

- Regulatory export limit (German §14a EnWG / EEG-65 % throttle).
- Large PV-to-battery ratio where summer curtailment is significant.
- Inverters that physically cap export but don't internally throttle PV
  (so curtailed energy is genuinely lost without our intervention).

## Hardware requirements

- A writable HA entity for "battery max charging power" in watts.
  Domain must be `number` or `input_number`.
- The entity must be active in normal self-consumption mode (i.e., the
  cap applies regardless of forced-charge mode).

### Inverters known to expose this

- **Sungrow SHx (SH5K, SH10RT, etc.)** with [mkaiser's Modbus integration](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant).
- **SMA Sunny Boy / Sunny Tripower** with the SMA Modbus / Sunny WebBox integration. Look for "Battery max charging power" or equivalent.

If your inverter is not listed, check whether the Modbus or
manufacturer-specific integration exposes a writable charge-power
register.

## Configuration walkthrough

In the integration's main config flow, scroll to the battery section.
You'll see a new "Dynamic Battery Charging" subsection:

1. Tick **Enable dynamic battery charging**.
2. Pick the **Inverter Battery Max Charge Power Entity** from the entity
   selector (filtered to `number` and `input_number`).
3. Set **Battery Max Charge Power (W)** to your inverter's maximum (e.g.,
   5000 for a 5 kW battery charge limit).
4. Leave **Battery Trickle Charge Power (W)** at the default 100 W unless
   your inverter rejects that.

Dependencies (validated at submit time):

- Battery capacity must be set.
- A battery SoC sensor must be wired.
- An export limit must be set (the soft ceiling for both this feature
  and the existing appliance scheduling).

If any are missing, the form re-displays with an error.

> **Note on forecast provider:** the form does **not** reject a
> missing forecast provider. The feature still activates, but only the
> reactive ceiling-follower will be active — the morning-throttle layer
> requires a forecast and silently degrades when none is configured.
> Status sensor will read `active`, `dynamic_battery_charge_planned_w`
> stays at the trickle value (100 W). To get the full feature, also
> configure a forecast provider (Solcast, Forecast.Solar, or Generic)
> in the main config flow.

## Status & troubleshooting

The integration's status sensor exposes four attributes:

| Attribute | Meaning |
|---|---|
| `dynamic_battery_charge_status` | `active` / `idle` / `paused: <reason>` / `disabled: <reason>` |
| `dynamic_battery_charge_setpoint_w` | Last value written to the inverter entity |
| `dynamic_battery_charge_planned_w` | Planned floor for the current hour (from the curve) |
| `dynamic_battery_charge_reactive_w` | Reactive lift contribution this cycle |

### Common `disabled: <reason>` values

| Reason | Fix |
|---|---|
| `entity_not_configured` | Set the inverter entity in the config flow. |
| `entity_not_found` | The configured entity doesn't exist in HA — check that the integration providing it is loaded. |
| `entity_wrong_domain` | Entity must be `number` or `input_number`. |
| `invalid_max_power` | Set a positive battery max charge power. |
| `invalid_export_limit` | Set a positive export limit. |
| `invalid_battery_capacity` | Set a positive battery capacity. |

### Why am I not seeing morning throttle?

The morning throttle only kicks in when forecast curtailment alone is
enough to fill the battery to its target SoC, and a forecast provider
is configured. Conditions that defeat it:

- **No forecast provider configured.** When `forecast_provider="none"`
  the planner does not run at all (`coordinator.py` early-returns). The
  curve is never built, the dispatcher's planned floor stays at trickle
  (100 W), and only the reactive ceiling-follower is active. To enable
  the morning-throttle layer, configure a forecast provider in the main
  config flow (Solcast, Forecast.Solar, or Generic).
- **Forecast provider is set but returns no hourly data** (e.g. Solcast
  not yet polled after restart). The planner runs, the curve falls
  back to a single setpoint at the configured max charge power, and
  the reactive lift becomes redundant for that cycle. Resolves
  automatically once the forecast sensor populates.
- **Cloudy day forecast** with no slot above the export ceiling — there
  is nothing to curtail, so morning charging proceeds at max.
- **Battery already near target SoC** — there is no headroom worth
  deferring; the curve falls into the `already_at_target` fallback.

In short: enable a forecast provider if you want the morning-throttle
behaviour; without one, the feature degrades cleanly to reactive-only.

## Interaction with existing features

| Feature | Behavior |
|---|---|
| `force_charge` switch | Loop pauses; cap reverts to max so forced-charge can charge fast. |
| `auto_battery_grid_charge` | Same — loop pauses on engagement. |
| `BatteryStrategy` | Strategy decides *how much* energy goes to the battery; this feature decides *when* during the day. Strategy is unchanged. |
| Phase 4 discharge protection | Independent — different physical register. |
| Cheap-tariff discharge block | Independent — discharge limiter doesn't affect charge cap. |
