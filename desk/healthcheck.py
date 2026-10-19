"""Is the bot alive? Exit code 0 = yes, 1 = no.  python -m desk.healthcheck

Used by Docker's health check (shown in `docker compose ps`). "Alive" means
the 1-minute price-check loop finished recently. That loop records a
heartbeat even when CoinSpot is down, so this measures the bot itself, not
the exchange. It only READS the database (and never creates one), so it
can't get in the bot's way.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from desk.clock import Clock, parse_iso
from desk.config import Config, load_config
from desk.db import Database


def heartbeat_age_seconds(db: Database, now: datetime, which: str = "fast") -> float | None:
    v = db.kv_get(f"heartbeat_{which}")
    return None if not v else (now - parse_iso(v)).total_seconds()


def read_heartbeat(db_path: Path, which: str = "fast") -> str | None:
    """The heartbeat timestamp, read with a read-only connection."""
    with closing(sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as c:
        row = c.execute("SELECT value FROM kv_state WHERE key = ?", (f"heartbeat_{which}",)).fetchone()
    return row[0] if row and row[0] else None


def health_limit_seconds(config: Config) -> float:
    """Same limit the bot's own hang detector uses."""
    return max(5 * config.schedule.fast_check_seconds, 300)


def is_healthy(age_seconds: float | None, config: Config) -> tuple[bool, str]:
    if age_seconds is None:
        return False, "the bot has never run on this database"
    if age_seconds > health_limit_seconds(config):
        return False, f"no price check for {age_seconds / 60:.0f} minutes"
    return True, f"last price check {age_seconds:.0f}s ago"


def check(config: Config, clock: Clock) -> tuple[bool, str]:
    path = config.path("database")
    if not path.exists():
        return False, "no database yet (the bot hasn't started)"
    beat = read_heartbeat(path)
    age = None if beat is None else (clock.now() - parse_iso(beat)).total_seconds()
    return is_healthy(age, config)


def main() -> int:
    try:
        config = load_config()
        ok, why = check(config, Clock(config.tz))
    except Exception as exc:          # a broken health check must report unhealthy, not crash
        print(f"unhealthy: {type(exc).__name__}: {exc}")
        return 1
    print(("healthy: " if ok else "unhealthy: ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
