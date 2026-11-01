# External electricity price forecast input

The forecasting model runs independently of this integration. Select its Home
Assistant sensor in **Configure → Energy Pricing → Electricity price forecast
sensor**. This setting is optional and independent of the tariff provider. Clear
the selector to remove the input. Completing the options flow reloads the entry.

Version 1 is for observation and evaluation. It creates a **Price Forecast**
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

- `schema_version` is the integer `1`, market `DE-LU`, unit `EUR/kWh`.
- `price_basis` is `wholesale` or `retail`. Wholesale prices exclude the household
  tariff's fees and taxes; the input does not perform that conversion.
- Every timestamp includes a timezone offset. UTC is recommended. Generation
  cannot be in the future, and expiration must be after generation and no more
  than six hours later. At expiration the snapshot is rejected.
- There are 1–672 chronological, contiguous intervals lasting 15, 30 or 60
  minutes. Each end is at most seven elapsed days after generation. At least one
  interval must still extend into the future. UTC comparisons preserve daylight
  saving transitions.
- Prices and bounds are finite JSON numbers with `lower <= price <= upper`.
  Zero and negative prices are valid. Missing numbers, booleans, NaN and infinity
  are rejected; missing data never becomes zero.
- `model` is a nonempty string of at most 128 characters. The supported quality
  is `experimental` with boolean `automation_eligible: false`. Bounds describe
  empirical spread, not demonstrated probability coverage.
- Additional metadata is allowed but is not copied into the accepted snapshot.

An independent producer can expose this JSON through a Home Assistant REST or
MQTT sensor. Producers can implement the same contract without adding forecasting
dependencies to this integration.

## Diagnostics and history

The diagnostic sensor reports `not_configured`, `missing_sensor`, `unavailable`,
`expired`, `no_future_intervals` or a validation reason such as
`unsupported_schema`, `unsupported_market_or_unit`, `unsupported_quality`,
`future_generation`, `invalid_price`, `invalid_bounds`, or
`noncontiguous_intervals`. Rejected input clears the accepted snapshot on the next
coordinator update; an old successful forecast is not retained as current.

Accepted attributes include `forecast_start`, `forecast_end`, `interval_count`,
the model's generation/expiration times and its intervals. The large `intervals`
attribute is excluded from Recorder for this diagnostic entity. Exclude the raw
source sensor from Recorder separately if it also carries a large curve.

Reliability evaluation and any future use of forecasts for battery decisions
belong to a separate development stage. This input does not extend the active
planner's horizon.
