"""HA-independent forecast adapters preserving interval energy and source identity."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from typing import Any

from .models import ForecastData, HourlyForecast


def _parse_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    """Normalize native and serialized timestamps to UTC; legacy naive means UTC."""
    try:
        result = (
            value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        )
        return (
            result.replace(tzinfo=timezone.utc)
            if result.tzinfo is None
            else result.astimezone(timezone.utc)
        )
    except (ValueError, TypeError):
        return None


def _source(states: dict, entity: str) -> tuple[float, dict]:
    source = states.get(entity, {})
    total = _parse_float(source.get("state"))
    if total is None:
        raise ValueError(f"Forecast unavailable: {entity}")
    attributes = source.get("attributes") or {}
    # Energy state units are explicit; do not infer from magnitude.
    if attributes.get("unit_of_measurement") == "Wh":
        total /= 1000
    return total, attributes


def _interval(start: datetime, end: datetime, watts: float) -> HourlyForecast:
    return HourlyForecast(
        start, end, watts * (end - start).total_seconds() / 3600000, watts
    )


def _combine(sources: list[list[HourlyForecast]]) -> list[HourlyForecast]:
    """Sum distinct sources; the last overlapping interval wins within a source."""
    boundaries = sorted({t for rows in sources for r in rows for t in (r.start, r.end)})
    result = []
    for start, end in pairwise(boundaries):
        active = [
            next((r for r in reversed(rows) if r.start <= start and r.end >= end), None)
            for rows in sources
        ]
        if any(r is not None for r in active):
            result.append(
                _interval(
                    start, end, sum(r.expected_watts for r in active if r is not None)
                )
            )
    return result


def _duration(value: Any, default: timedelta) -> timedelta:
    if isinstance(value, timedelta):
        return value if value.total_seconds() > 0 else default
    match = re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", str(value)
    )
    if match:
        seconds = sum(
            float(v or 0) * scale for v, scale in zip(match.groups(), [3600, 60, 1])
        )
        if seconds > 0:
            return timedelta(seconds=seconds)
    return default


class ForecastProvider(ABC):
    @abstractmethod
    def get_forecast(self, states: dict[str, dict]) -> ForecastData:
        """Return complete forecast data or raise ValueError for missing input."""


class GenericForecastProvider(ForecastProvider):
    """Remaining energy totals only; no invented production timing."""

    def __init__(self, forecast_entity: str) -> None:
        self.forecast_entity = forecast_entity

    def get_forecast(self, states: dict[str, dict]) -> ForecastData:
        total, _ = _source(states, self.forecast_entity)
        return ForecastData(remaining_today_kwh=total)


class SolcastProvider(GenericForecastProvider):
    def get_forecast(self, states: dict[str, dict]) -> ForecastData:
        total, attrs = _source(states, self.forecast_entity)
        key = next(
            (
                k
                for k in ("forecasts", "detailedForecast", "detailedHourly")
                if attrs.get(k)
            ),
            None,
        )
        duration = (
            timedelta(hours=1) if key == "detailedHourly" else timedelta(minutes=30)
        )
        rows = self._parse_forecasts(attrs.get(key, []) if key else [], duration)
        return ForecastData(total, rows, _parse_float(attrs.get("forecast_tomorrow")))

    def _parse_forecasts(
        self, forecasts_raw: list[dict], duration: timedelta = timedelta(minutes=30)
    ) -> list[HourlyForecast]:
        rows = []
        for slot in forecasts_raw:
            if not isinstance(slot, dict):
                continue
            start = _parse_iso(slot.get("period_start"))
            end = _parse_iso(slot.get("period_end"))
            span = _duration(slot.get("period"), duration)
            if start is None and end is not None:
                start = end - span
            watts = _parse_float(slot.get("pv_estimate"))
            if start is None or watts is None:
                continue
            end = end or start + span
            if end > start:
                rows.append(_interval(start, end, watts * 1000))
        return _combine([rows])


class ForecastSolarProvider(GenericForecastProvider):
    """Read interval-start watts mappings and daily energy totals."""

    def get_forecast(
        self, states: dict[str, dict], *, now: datetime | None = None
    ) -> ForecastData:
        total, attrs = _source(states, self.forecast_entity)
        rows = self._parse_watts(attrs.get("watts") or {})
        tomorrow = (now or datetime.now(timezone.utc)).date() + timedelta(days=1)
        tomorrow_total = None
        for key, value in (attrs.get("wh_days") or {}).items():
            try:
                day = (
                    key.date()
                    if isinstance(key, datetime)
                    else date.fromisoformat(str(key)[:10])
                )
            except (TypeError, ValueError):
                continue
            if day == tomorrow and (wh := _parse_float(value)) is not None:
                tomorrow_total = wh / 1000
        return ForecastData(total, rows, tomorrow_total)

    def _parse_watts(self, watts_dict: dict) -> list[HourlyForecast]:
        points = sorted(
            {
                dt: watts
                for key, value in watts_dict.items()
                if (dt := _parse_iso(key)) is not None
                and (watts := _parse_float(value)) is not None
            }.items()
        )
        rows = []
        for i, (start, watts) in enumerate(points):
            # Preserve legacy one-hour cadence when no following point exists;
            # never bridge an overnight gap with daytime production.
            cadence = (
                min(start - points[i - 1][0], timedelta(hours=1))
                if i
                else timedelta(hours=1)
            )
            end = (
                min(start + timedelta(hours=1), points[i + 1][0])
                if i + 1 < len(points)
                else start + cadence
            )
            rows.append(_interval(start, end, watts))
        return rows


class AggregatingForecastProvider(ForecastProvider):
    """Aggregate separate array entities; explicit tomorrow feeds replace that day."""

    def __init__(
        self,
        provider_type: str,
        entities: list[str],
        tomorrow_entities: list[str] | None = None,
    ) -> None:
        self.provider_type = provider_type
        self.entities = list(dict.fromkeys(e for e in entities if e))
        self.tomorrow_entities = list(
            dict.fromkeys(e for e in (tomorrow_entities or []) if e)
        )

    def get_forecast(
        self, states: dict[str, dict], *, now: datetime | None = None
    ) -> ForecastData:
        now = now or datetime.now(timezone.utc)

        def read(entity):
            provider = create_forecast_provider(self.provider_type, entity)
            return (
                provider.get_forecast(states, now=now)
                if isinstance(provider, ForecastSolarProvider)
                else provider.get_forecast(states)
            )

        if not self.entities:
            raise ValueError("Forecast unavailable: no sources configured")
        primary = [read(e) for e in self.entities]
        tomorrow = [read(e) for e in self.tomorrow_entities]
        tomorrow_start = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo
        ).astimezone(timezone.utc)
        tomorrow_end = datetime.combine(
            now.date() + timedelta(days=2), datetime.min.time(), now.tzinfo
        ).astimezone(timezone.utc)

        def clipped(rows, replace_tomorrow):
            out = []
            for r in rows:
                segments = (
                    [
                        (r.start, min(r.end, tomorrow_start)),
                        (max(r.start, tomorrow_end), r.end),
                    ]
                    if replace_tomorrow
                    else [(max(r.start, tomorrow_start), min(r.end, tomorrow_end))]
                )
                out.extend(
                    _interval(a, b, r.expected_watts) for a, b in segments if b > a
                )
            return out

        sources = [
            clipped(d.hourly_breakdown, True) if tomorrow else d.hourly_breakdown
            for d in primary
        ]
        sources.extend(clipped(d.hourly_breakdown, False) for d in tomorrow)
        tomorrow_total = (
            sum(d.remaining_today_kwh for d in tomorrow)
            if tomorrow
            else sum(d.tomorrow_total_kwh for d in primary)
            if all(d.tomorrow_total_kwh is not None for d in primary)
            else None
        )
        return ForecastData(
            sum(d.remaining_today_kwh for d in primary),
            _combine(sources),
            tomorrow_total,
        )


def create_forecast_provider(
    provider_type: str, forecast_entity: str
) -> ForecastProvider:
    mapping = {
        "none": GenericForecastProvider,
        "generic": GenericForecastProvider,
        "solcast": SolcastProvider,
        "forecast_solar": ForecastSolarProvider,
    }
    if provider_type not in mapping:
        raise ValueError(f"Unknown forecast provider type: {provider_type!r}")
    return mapping[provider_type](forecast_entity)
