"""Daily clean-up so the database can't slowly fill the disk.

Deleted (they're only useful for a short while):
  - scanner_signals  older than 14 days  (the biggest table in day mode)
  - price_snapshots  older than 30 days
  - news_items       older than 60 days
Never deleted: trades, positions, orders, debates, risk decisions, AI calls,
equity history: your records (and tax history) stay forever.
"""

from __future__ import annotations

from datetime import timedelta

from desk.clock import Clock, iso
from desk.db import Database

RETENTION_DAYS = {"scanner_signals": 14, "price_snapshots": 30}
NEWS_RETENTION_DAYS = 60


def prune(db: Database, clock: Clock) -> dict[str, int]:
    now = clock.now()
    removed = {}
    with db.tx() as t:
        for table, days in RETENTION_DAYS.items():
            cur = t.execute(f"DELETE FROM {table} WHERE ts < ?", (iso(now - timedelta(days=days)),))
            removed[table] = cur.rowcount
        cur = t.execute("DELETE FROM news_items WHERE fetched_at < ?",
                        (iso(now - timedelta(days=NEWS_RETENTION_DAYS)),))
        removed["news_items"] = cur.rowcount
    return removed


def run_daily(db: Database, clock: Clock) -> dict[str, int] | None:
    """Prune at most once per local day. Returns what was removed, or None if already done today."""
    key = f"housekeeping_done:{clock.local_date().isoformat()}"
    if db.kv_get(key):
        return None
    db.kv_set(key, "1")              # marked first: if pruning fails it's retried tomorrow, not every minute
    removed = prune(db, clock)
    if any(removed.values()):
        db.log_event("housekeeping", "Removed old records: " + ", ".join(f"{k} {v}" for k, v in removed.items()))
    return removed
