"""Short-term estimates for household demand without a separate meter."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite


@dataclass(frozen=True)
class HouseholdLoadProfile:
    """Resolution of measured hourly demand, without synthetic observations."""

    watts: tuple[float, ...] | None = None
    source: str = "unavailable"
    retained_hours: tuple[int, ...] = ()
    oldest_age_seconds: float | None = None
    reason: str | None = None


def resolve_household_profile(rows, now: datetime) -> HouseholdLoadProfile:
    """Retain at most six mature hours for a second week of telemetry gaps.

    Seven days remains the freshness threshold. Fourteen days is a hard age
    limit, and at least eighteen hours must still be fresh. These are bounded
    availability guardrails, not a forecast accuracy guarantee. No stored
    values, observation counts or timestamps are changed by this resolver.
    """
    if not isinstance(rows, (list, tuple)) or len(rows) != 24:
        return HouseholdLoadProfile(reason="incomplete")
    watts, ages = [], []
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError
        for row in rows:
            value, seconds, stamp = row
            if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v)
                    for v in (value, seconds)) or value < 0 or seconds < 1800):
                raise ValueError
            observed = datetime.fromisoformat(stamp)
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError
            age = (now.astimezone(timezone.utc)-observed.astimezone(timezone.utc)).total_seconds()
            if age < 0:
                raise ValueError
            watts.append(value)
            ages.append(age)
    except (ValueError, TypeError, OverflowError):
        return HouseholdLoadProfile(reason="incomplete")
    retained = tuple(hour for hour, age in enumerate(ages) if age > 7*86400)
    oldest = max(ages)
    if oldest > 14*86400 or len(retained) > 6:
        return HouseholdLoadProfile(retained_hours=retained, oldest_age_seconds=oldest,
                                    reason="expired")
    return HouseholdLoadProfile(tuple(watts), "retained" if retained else "learned", retained, oldest)


class RecentHouseholdLoad:
    """Time-weight demand over two minutes of distinct observed reports.

    Each reading describes the interval until the next report. A fixed prior
    fills the unobserved portion during warm-up; duplicate timestamps add no
    information. Callers must reset on missing inputs or changed meter mappings.
    This estimate is for forecasting, never for real-time safety or accounting.

    Excess demand earns a two, five, ten or thirty minute forecast after the
    corresponding observed duration (two minutes is the initial bound). A
    background increase needs thirty minutes before it can support the longer
    outlook of a separately metered run. That reference falls with the recent
    mean, and the caller still caps it by each future hour's learned profile.
    """

    WINDOW_SECONDS = 120.0

    def __init__(self) -> None:
        self._segments: deque[tuple[datetime, datetime, float]] = deque()
        self._last_at: datetime | None = None
        self._last_watts = 0.0
        self._prior_watts = 0.0
        self._estimate = 0.0
        self._background_reference = 0.0
        self._background_since: datetime | None = None
        self._excess_since: datetime | None = None
        self._persistence_seconds = self.WINDOW_SECONDS

    @property
    def persistence_seconds(self) -> float:
        """Forecast duration earned by distinct observations, bounded to 30 min."""
        return self._persistence_seconds

    @property
    def background_reference_watts(self) -> float:
        """Established background for bounded known runs after the excess ends."""
        return self._background_reference

    def reset(self) -> None:
        """Discard history that no longer describes the current input source."""
        self._segments.clear()
        self._last_at = None
        self._last_watts = self._prior_watts = self._estimate = 0.0
        self._background_reference = 0.0
        self._background_since = self._excess_since = None
        self._persistence_seconds = self.WINDOW_SECONDS

    def observe(self, observed_at: datetime, watts: float, prior_watts: float,
                *, reference_watts: float | None = None) -> float:
        """Return the estimate at a real report's timestamp, normalized to UTC.

        Gaps exceeding the window or backwards timestamps start a fresh warm-up.
        Invalid data clears history and raises instead of inventing a zero load.
        ``reference_watts`` is the hourly profile allowance for this unknown
        component; it stays separate from the established background reference.
        """
        reference_watts = prior_watts if reference_watts is None else reference_watts
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
                for value in (watts, prior_watts, reference_watts)
            )
        ):
            self.reset()
            raise ValueError('An aware timestamp and finite nonnegative loads are required')
        observed_at = observed_at.astimezone(timezone.utc)
        if self._last_at is not None:
            elapsed = (observed_at - self._last_at).total_seconds()
            if elapsed < 0 or elapsed > self.WINDOW_SECONDS:
                self.reset()
            elif elapsed == 0:
                return self._estimate
        if self._last_at is None:
            self._last_at = observed_at
            self._last_watts = float(watts)
            self._prior_watts = self._estimate = float(prior_watts)
            self._background_reference = self._estimate
            return self._estimate

        previous_at = self._last_at
        start = self._last_at
        if self._segments and self._segments[-1][2] == self._last_watts:
            start, _, _ = self._segments.pop()
        self._segments.append((start, observed_at, self._last_watts))
        cutoff = observed_at - timedelta(seconds=self.WINDOW_SECONDS)
        while self._segments and self._segments[0][1] <= cutoff:
            self._segments.popleft()
        known_seconds = 0.0
        watt_seconds = 0.0
        for start, end, load in self._segments:
            duration = (end - max(start, cutoff)).total_seconds()
            known_seconds += duration
            watt_seconds += load * duration
        watt_seconds += max(0.0, self.WINDOW_SECONDS - known_seconds) * self._prior_watts
        self._estimate = watt_seconds / self.WINDOW_SECONDS
        # A lower recent mean reduces the reference without discarding energy
        # from duty-cycle loads. Only sustained observations can raise it.
        self._background_reference = min(self._background_reference, self._estimate)
        for attribute, reference in (
            ("_background_since", self._background_reference),
            ("_excess_since", reference_watts),
        ):
            if round(self._estimate / 100) > round(reference / 100):
                if getattr(self, attribute) is None:
                    setattr(self, attribute, previous_at)
            else:
                setattr(self, attribute, None)
        starts = [stamp for stamp in (self._background_since, self._excess_since)
                  if stamp is not None]
        duration = (observed_at - min(starts)).total_seconds() if starts else 0.
        self._persistence_seconds = next(
            (seconds for seconds in (1800., 600., 300.) if duration >= seconds),
            self.WINDOW_SECONDS,
        )
        if (self._background_since is not None
                and (observed_at - self._background_since).total_seconds() >= 1800):
            self._background_reference = self._estimate
            self._background_since = None
        self._last_at, self._last_watts = observed_at, float(watts)
        return self._estimate
