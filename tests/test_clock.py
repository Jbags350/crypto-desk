from datetime import UTC, datetime

from desk.clock import FakeClock, iso, parse_iso


def test_local_day_start_sydney():
    # 2026-10-09 08:00 UTC = 19:00 AEDT (UTC+11) on the 9th
    c = FakeClock(datetime(2026, 10, 9, 8, 0, tzinfo=UTC), tz="Australia/Sydney")
    assert c.local_date().isoformat() == "2026-10-09"
    assert c.day_start_utc() == datetime(2026, 10, 8, 13, 0, tzinfo=UTC)
    assert c.next_day_start_utc() == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


def test_local_midnight_rollover():
    c = FakeClock(datetime(2026, 10, 9, 12, 59, tzinfo=UTC), tz="Australia/Sydney")
    assert c.local_date().isoformat() == "2026-10-09"
    c.advance(minutes=2)
    assert c.local_date().isoformat() == "2026-10-10"


def test_iso_roundtrip():
    dt = datetime(2026, 10, 9, 8, 14, 38, 327000, tzinfo=UTC)
    assert iso(dt) == "2026-10-09T08:14:38.327+00:00"
    assert parse_iso(iso(dt)) == dt
    assert parse_iso("2026-10-09T08:14:38.327Z") == dt
