"""Time helpers. All stored times are UTC; "days" use your local time zone.

Code asks a Clock for the time instead of calling datetime.now() directly,
so tests can freeze or fast-forward time (e.g. to test the midnight reset of
the daily loss limit).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class Clock:
    def __init__(self, tz: ZoneInfo | str = "UTC"):
        self.tz = ZoneInfo(tz) if isinstance(tz, str) else tz

    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        return datetime.now(UTC)

    def local_now(self) -> datetime:
        return self.now().astimezone(self.tz)

    def local_date(self) -> date:
        return self.local_now().date()

    def day_start_utc(self, day: date | None = None) -> datetime:
        """Midnight (local time) at the start of `day`, as UTC."""
        day = day or self.local_date()
        return datetime.combine(day, time(0, 0), tzinfo=self.tz).astimezone(UTC)

    def next_day_start_utc(self) -> datetime:
        return self.day_start_utc(self.local_date() + timedelta(days=1))


class FakeClock(Clock):
    """A clock you control, for tests."""

    def __init__(self, start: datetime, tz: ZoneInfo | str = "UTC"):
        super().__init__(tz)
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start time")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(UTC)

    def advance(self, seconds: float = 0, **kwargs: float) -> None:
        self._now += timedelta(seconds=seconds, **kwargs)


def iso(dt: datetime) -> str:
    """UTC ISO-8601 text for the database, e.g. 2026-10-09T08:14:38.327+00:00."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
