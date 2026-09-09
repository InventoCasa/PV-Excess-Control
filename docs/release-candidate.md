# 0.4.0rc1 release candidate

This candidate adds reviewed control fixes and optional planning features. It is
undergoing a production pilot; stable 0.4.0 will follow the pilot's results.

## Changes

- Persistent daily counters, correct appliance device ownership and explicit
  Enabled/Paused/Override behavior, including retryable shutdown.
- Consistent hybrid and Battery First accounting and separate solar/grid budgets.
- Timestamp-based averaging, optional current increase interval/minimum change,
  confirmed-command deduplication and disconnected-EV presets.
- Start/run permission sensors, continuous solar start delay, observed phase
  counts, positive export buffers and writable price/grid controls.
- Forecast aggregation for multiple arrays, exact interval durations and explicit
  tomorrow-source replacement; unavailable sources remain visible.
- Remaining-runtime demand and bounded continuous runs, with partial scheduling
  that does not reserve the same solar power twice.
- Battery charge-cap release after restart or temporary actuator failure,
  including recovery of devices that appear later during HA startup.

## Upgrade notes

Home Assistant 2025.8 or newer is required. Existing configuration and entity IDs
are retained. New options default to unset/off. Price thresholds use complete
end-customer currency/kWh values, including negative prices.

Positive off_threshold now consistently retains the configured solar buffer.
A remaining-runtime sensor supplies minutes still needed and cannot be combined
with a positive fixed daily minimum. Continuous requests require a daily maximum.
Explicit tomorrow forecasts must contain all tomorrow arrays because they replace
the primary sources' tomorrow portion. Avoid combining an already aggregated
forecast with its component arrays.

If upgrading from the original blueprint/pyscript controller, disable the old
controller before enabling this integration for the same appliances.

## Validation

The release candidate was checked on the minimum and current supported test
stacks, exercised in 49 HA service-level scenarios, and observed for about 15hours
including local midnight without unexpected availability failures. The planned
24-hour observation was stopped early by user acceptance; it is not represented
as a completed 24-hour test. See [testing](testing.md).

The original hardware-specific [issue #57](https://github.com/InventoCasa/PV-Excess-Control/issues/57)
still needs field diagnostics. Active hardware phase switching, unmeasured
zero-export reserve discovery and device-specific thermostat logic are outside
this candidate.

Related guides: [controls](generic-controls.md), [forecast/runtime](forecast-runtime.md),
[battery charging](dynamic-battery-charging.md), [restart behavior](stabilization.md).
