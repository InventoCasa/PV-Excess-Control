"""Short-term estimates for household demand without a separate meter."""
from collections import deque
from datetime import datetime, timedelta, timezone
from math import isfinite


class RecentHouseholdLoad:
    """Time-weight demand over two minutes of distinct observed reports.

    Each reading describes the interval until the next report. A fixed prior
    fills the unobserved portion during warm-up; duplicate timestamps add no
    information. Callers must reset on missing inputs or changed meter mappings.
    This estimate is for forecasting, never for real-time safety or accounting.
    """

    WINDOW_SECONDS = 120.0

    def __init__(self) -> None:
        self._segments: deque[tuple[datetime, datetime, float]] = deque()
        self._last_at: datetime | None = None
        self._last_watts = 0.0
        self._prior_watts = 0.0
        self._estimate = 0.0

    def reset(self) -> None:
        """Discard history that no longer describes the current input source."""
        self._segments.clear()
        self._last_at = None
        self._last_watts = self._prior_watts = self._estimate = 0.0

    def observe(self, observed_at: datetime, watts: float, prior_watts: float) -> float:
        """Return the estimate at a real report's timestamp, normalized to UTC.

        Gaps exceeding the window or backwards timestamps start a fresh warm-up.
        Invalid data clears history and raises instead of inventing a zero load.
        """
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
                for value in (watts, prior_watts)
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
            return self._estimate

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
        self._last_at, self._last_watts = observed_at, float(watts)
        return self._estimate
