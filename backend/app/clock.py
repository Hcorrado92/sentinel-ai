"""Injectable clock so that time-dependent logic is testable."""

from datetime import datetime, timedelta, timezone
from typing import Protocol

from fastapi import Request


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Deterministic clock for tests; time only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        start = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._now = value

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        self._now = self._now + (delta if delta is not None else timedelta(**kwargs))
        return self._now


def get_clock(request: Request) -> Clock:
    """FastAPI dependency returning the app's clock."""
    return request.app.state.clock
