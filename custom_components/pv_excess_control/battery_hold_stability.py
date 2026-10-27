"""Bounded confirmation of an economic release, independent of device access."""
from datetime import datetime, timedelta, timezone
from math import isfinite


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('An aware timestamp is required')
    return value.astimezone(timezone.utc)


class HoldReleaseConfirmation:
    """Briefly retain a confirmed hold while a native proposal settles.

    The caller supplies an immutable context describing hard planning inputs.
    It must independently validate live measurements, ownership and safety.
    This filter authorizes no device operation and changes no economic plan.
    Only ``authorize`` records a freshly confirmed hold; repeated proposals
    cannot extend its end or restart a completed confirmation interval.
    """

    def __init__(self, delay_seconds: float = 60.0) -> None:
        if (isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float))
                or not isfinite(delay_seconds) or not 0 < delay_seconds <= 60):
            raise ValueError('Release confirmation must be between zero and sixty seconds')
        self._delay = timedelta(seconds=delay_seconds)
        self.authorized_until: datetime | None = None
        self.context: object | None = None
        self.pending_until: datetime | None = None
        self._last_now: datetime | None = None

    def reset(self) -> None:
        """Discard a lease when control or its validated context changes."""
        self.authorized_until = self.context = self.pending_until = None
        self._last_now = None

    def _observe(self, now: datetime) -> datetime:
        instant = _utc(now)
        if self._last_now is not None and instant < self._last_now:
            raise ValueError('Time moved backwards')
        self._last_now = instant
        return instant

    def authorize(self, now: datetime, until: datetime, context: object) -> bool:
        """Record a physically confirmed hold, never an unexecuted proposal."""
        try:
            instant, end = self._observe(now), _utc(until)
            if end <= instant:
                raise ValueError('Hold authorization has expired')
        except (ValueError, TypeError, OverflowError):
            self.reset()
            return False
        self.authorized_until = end
        self.context = context
        self.pending_until = None
        return True

    def propose(self, now: datetime, action: str, context: object,
                window_end: datetime) -> datetime | None:
        """Return the existing hold's bounded release deadline, if any."""
        try:
            instant, boundary = self._observe(now), _utc(window_end)
            if (self.authorized_until is None or self.context != context
                    or instant >= min(self.authorized_until, boundary)
                    or self.pending_until is not None and instant >= self.pending_until):
                self.reset()
                return None
            if action == 'hold':
                self.pending_until = None
                return None
            if action != 'self_consumption':
                self.reset()
                return None
            pending = self.pending_until or instant + self._delay
            self.pending_until = min(pending, self.authorized_until, boundary)
            return self.pending_until
        except (ValueError, TypeError, OverflowError):
            self.reset()
            return None
