"""Validate external electricity price forecasts without Home Assistant access.

Versions 1 and 2 are observation contracts. Version 2 also represents point
forecasts with explicitly absent bounds. Accepted snapshots never grant device
control, and supplied bounds are not calibrated probabilities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any


class PriceForecastError(ValueError):
    """A rejected snapshot, with a stable diagnostic reason."""


@dataclass(frozen=True)
class PriceForecastInterval:
    """One immutable interval with optional explicit bounds in EUR/kWh."""

    start: datetime
    end: datetime
    price: float
    lower: float | None
    upper: float | None
    source: str | None = None

    def as_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        """Return an independent JSON-compatible representation."""
        result = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "price": self.price,
            "lower": self.lower,
            "upper": self.upper,
        }
        if include_source and self.source is not None:
            result["source"] = self.source
        return result


@dataclass(frozen=True)
class PriceForecastSnapshot:
    """An accepted forecast, deliberately distinct from a confirmed tariff."""

    generated_at: datetime
    expires_at: datetime
    model: str
    price_basis: str
    intervals: tuple[PriceForecastInterval, ...]
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        """Return only validated contract fields, without source-owned objects."""
        return {
            "schema_version": self.schema_version,
            "market": "DE-LU",
            "unit": "EUR/kWh",
            "price_basis": self.price_basis,
            "generated_at": self.generated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "model": self.model,
            "quality": {"status": "experimental", "automation_eligible": False},
            "intervals": [row.as_dict(include_source=self.schema_version == 2) for row in self.intervals],
        }


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise PriceForecastError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("missing offset")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as err:
        raise PriceForecastError("invalid_timestamp") from err


def _number(value: Any) -> float:
    if type(value) not in (int, float):
        raise PriceForecastError("invalid_price")
    try:
        number = float(value)
    except OverflowError as err:
        raise PriceForecastError("invalid_price") from err
    if not math.isfinite(number):
        raise PriceForecastError("invalid_price")
    return number


def parse_price_forecast(payload: Any, *, now: datetime) -> PriceForecastSnapshot:
    """Validate version, meaning, freshness and interval coverage atomically.

    Missing, malformed or expired data raises ``PriceForecastError``. Zero and
    negative prices are retained. Extra metadata is ignored rather than copied
    into the accepted snapshot. All duration comparisons use UTC.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise PriceForecastError("invalid_now")
    now = now.astimezone(timezone.utc)
    if not isinstance(payload, Mapping):
        raise PriceForecastError("invalid_payload")
    version = payload.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise PriceForecastError("unsupported_schema")
    if payload.get("market") != "DE-LU" or payload.get("unit") != "EUR/kWh":
        raise PriceForecastError("unsupported_market_or_unit")
    if payload.get("price_basis") not in ("wholesale", "retail"):
        raise PriceForecastError("unsupported_price_basis")
    quality = payload.get("quality")
    if (
        not isinstance(quality, Mapping)
        or quality.get("status") != "experimental"
        or quality.get("automation_eligible") is not False
    ):
        raise PriceForecastError("unsupported_quality")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise PriceForecastError("invalid_model")
    generated = _timestamp(payload.get("generated_at"))
    expires = _timestamp(payload.get("expires_at"))
    if generated > now:
        raise PriceForecastError("future_generation")
    if not timedelta() < expires - generated <= timedelta(hours=6):
        raise PriceForecastError("invalid_expiration")
    if now >= expires:
        raise PriceForecastError("expired")
    rows = payload.get("intervals")
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 672:
        raise PriceForecastError("invalid_interval_count")
    intervals = []
    previous_end = None
    for row in rows:
        if not isinstance(row, Mapping):
            raise PriceForecastError("invalid_interval")
        start, end = _timestamp(row.get("start")), _timestamp(row.get("end"))
        if (end - start).total_seconds() not in (900, 1800, 3600):
            raise PriceForecastError("invalid_interval_duration")
        if previous_end is not None and start != previous_end:
            raise PriceForecastError("noncontiguous_intervals")
        if end - generated > timedelta(days=7):
            raise PriceForecastError("horizon_too_long")
        price = _number(row.get("price"))
        source = None
        if version == 2:
            source = row.get("source")
            if not isinstance(source, str) or not source.strip() or len(source) > 128:
                raise PriceForecastError("invalid_source")
            if "lower" not in row or "upper" not in row:
                raise PriceForecastError("invalid_bounds")
            if (row["lower"] is None) != (row["upper"] is None):
                raise PriceForecastError("invalid_bounds")
        if version == 2 and row["lower"] is None and row["upper"] is None:
            lower = upper = None
        else:
            lower, upper = (_number(row.get(key)) for key in ("lower", "upper"))
            if not lower <= price <= upper:
                raise PriceForecastError("invalid_bounds")
        intervals.append(PriceForecastInterval(start, end, price, lower, upper, source))
        previous_end = end
    if intervals[-1].end <= now:
        raise PriceForecastError("no_future_intervals")
    return PriceForecastSnapshot(generated, expires, model, payload["price_basis"], tuple(intervals), version)
