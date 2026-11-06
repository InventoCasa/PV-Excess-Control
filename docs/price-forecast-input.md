# External electricity price forecast input

The forecasting model runs independently of this integration. Select its Home
Assistant sensor in **Configure → Energy Pricing → Electricity price forecast
sensor**. This setting is optional and independent of the tariff provider. Clear
the selector to remove the input. Completing the options flow reloads the entry.

Versions 1 and 2 are for observation and evaluation. They provide a **Price Forecast**
diagnostic sensor with source, model, timestamps and accepted intervals. A status
of `available` means that the input passed structural and freshness validation;
it does not certify prediction accuracy. Forecasts never replace confirmed tariff
windows, start charging, change a battery target or authorize appliance control.

## Sensor contract

The source sensor's attributes must contain this JSON object. Its state can be
`experimental` or another available value; `unknown` and `unavailable` are always
rejected. Values below illustrate the format, not a current forecast.

```json
{
  "schema_version": 1,
  "market": "DE-LU",
  "unit": "EUR/kWh",
  "price_basis": "wholesale",
  "generated_at": "2026-11-01T12:00:00+00:00",
  "expires_at": "2026-11-01T18:00:00+00:00",
  "model": "calendar-v1",
  "quality": {"status": "experimental", "automation_eligible": false},
  "intervals": [
    {
      "start": "2026-11-01T13:00:00+00:00",
      "end": "2026-11-01T14:00:00+00:00",
      "price": 0.08,
      "lower": 0.02,
      "upper": 0.14
    }
  ]
}
```

- `schema_version` is the integer `1` or `2`, market `DE-LU`, unit `EUR/kWh`.
- `price_basis` is `wholesale` or `retail`. Wholesale prices exclude the household
  tariff's fees and taxes; the input does not perform that conversion.
- Every timestamp includes a timezone offset. UTC is recommended. Generation
  cannot be in the future, and expiration must be after generation and no more
  than six hours later. At expiration the snapshot is rejected.
- There are 1–672 chronological, contiguous intervals lasting 15, 30 or 60
  minutes. Each end is at most seven elapsed days after generation. At least one
  interval must still extend into the future. UTC comparisons preserve daylight
  saving transitions.
- Prices are finite JSON numbers; zero and negative prices are valid. Version 1
  requires finite bounds with `lower <= price <= upper`. Version 2 allows either
  those bounds or explicit `lower: null` and `upper: null` together. Both bound
  keys remain required. Missing prices, booleans, NaN and infinity are rejected.
- Every version 2 interval also requires a nonempty `source` string of at most
  128 characters. This is an opaque producer label copied into diagnostics;
  the integration does not interpret it or use it to authorize control.
- `model` is a nonempty string of at most 128 characters. The supported quality
  is `experimental` with boolean `automation_eligible: false`. Bounds describe
  empirical spread, not demonstrated probability coverage.
- Additional metadata is allowed but is not copied into the accepted snapshot.

An independent producer can expose this JSON through a Home Assistant REST or
MQTT sensor. Producers can implement the same contract without adding forecasting
dependencies to this integration.

## Point forecasts in version 2

Keep the same envelope, set `schema_version` to `2`, and provide `source` on every
interval. An interval without a supplied uncertainty band looks like this:

```json
{
  "start": "2026-11-01T13:00:00+00:00",
  "end": "2026-11-01T14:00:00+00:00",
  "price": 0.08,
  "lower": null,
  "upper": null,
  "source": "external-provider"
}
```

Display this price without a band. The integration preserves both nulls and the
source label. A version 2 payload may combine point and bounded intervals; one
null bound paired with a numeric bound is invalid. Version 1 keeps its existing
required numeric bounds and output shape. The same freshness, coverage and
observation-only rules apply to both versions.

## Diagnostics and history

The diagnostic sensor reports `not_configured`, `missing_sensor`, `unavailable`,
`expired`, `no_future_intervals` or a validation reason such as
`unsupported_schema`, `unsupported_market_or_unit`, `unsupported_quality`,
`future_generation`, `invalid_price`, `invalid_bounds`, `invalid_source`, or
`noncontiguous_intervals`. Rejected input clears the accepted snapshot on the next
coordinator update; an old successful forecast is not retained as current.

Accepted attributes include `forecast_start`, `forecast_end`, `interval_count`,
the model's generation/expiration times and its intervals. The large `intervals`
attribute is excluded from Recorder for this diagnostic entity. Exclude the raw
source sensor from Recorder separately if it also carries a large curve.

Reliability evaluation and any future use of forecasts for battery decisions
belong to a separate development stage. This input does not extend the active
planner's horizon.
