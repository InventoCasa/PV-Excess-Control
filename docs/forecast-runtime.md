# Forecast sources and remaining runtime

## Multiple arrays and tomorrow forecasts

Keep the existing `forecast_sensor` and add distinct array sensors through
`additional_forecast_sensors`. Their energy totals and overlapping production
intervals are summed. Duplicate entity IDs are counted once; duplicate or
overlapping intervals within one source are not added twice. Do not combine an
already aggregated total sensor with its individual component arrays.

`forecast_tomorrow_sensor` and `additional_forecast_tomorrow_sensors` form an
explicit set for tomorrow. When supplied, they replace tomorrow's part of the
primary sources. Supply the complete set of tomorrow arrays, not only one extra
array. The HA local date determines tomorrow, including DST transitions.

Solcast `detailedForecast` supplies average kW over half-hours; `detailedHourly`
supplies hourly average kW. Explicit period/end fields take precedence. The adapter
preserves interval boundaries and derives kWh from the real duration. Native HA
datetime attributes and ISO timestamps are accepted; legacy naive timestamps mean
UTC. The provider formats are documented by the
[Solcast integration](https://github.com/BJReplay/ha-solcast-solar).

Forecast.Solar interval-start `watts` mappings use the next timestamp, capped at
one hour to avoid spanning overnight gaps. A trailing point uses the preceding
cadence; an isolated point retains the legacy one-hour compatibility assumption.
Prefer complete feeds with an explicit final zero. Its
[provider model](https://github.com/home-assistant-libs/forecast_solar/blob/master/src/forecast_solar/models.py)
also exposes daily energy totals. Native HA forecast entities that expose only
a daily total remain totals-only; this integration cannot reconstruct a missing
detailed curve. Generic sources likewise provide totals without invented timing.

Missing, unavailable or non-finite configured sources invalidate the combined
forecast. They must not appear as a sunny forecast with a missing array silently
set to zero. Controller operation from valid live measurements remains available;
forecast-dependent planning and battery targets require complete inputs.

## Remaining-runtime demand

`remaining_runtime_entity` accepts a sensor or input helper describing the number
of **minutes still needed**. An HA template can translate temperature or process
requirements into this value; the integration does not add its own thermostat.
It is mutually exclusive with a positive fixed `min_daily_runtime`.

The demand is capped by the daily maximum still available. Zero ends the request;
`on_only` retains supply after completion. An unknown value blocks new automatic
starts and does not mean the task completed. `require_contiguous_runtime` requests
a continuous run once started and requires a positive daily maximum. Existing
maximum-runtime, time-window and battery/EV safety limits still apply. A new demand
value updates planning rather than waiting for the normal planner interval.

For explicitly continuous requests, ON time counts toward the daily maximum even
below a configured completion-power threshold. This bounds a stuck or unavailable
remaining-runtime source that draws zero watts. Plan Confidence exposes source
status, today/tomorrow totals and interval count. A configured forecast outage
pauses dynamic battery throttling and yields the maximum charge cap once; source
recovery replans at controller cadence.

A missing configured forecast after restart also releases a retained cap after the
startup grace. Failed/unavailable releases remain pending until accepted; a late
inverter entity can recover without another reload. The Status attribute
`dynamic_battery_charge_release_pending` identifies an unfinished release.
