"""SQLite database: the bot's permanent memory.

- WAL mode lets the bot, dashboard and watchdog read/write at the same time.
- Each thread gets its own connection (SQLite connections aren't shareable).
- The schema is upgraded with numbered migrations, so later stages can add
  tables without losing existing records.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from desk.clock import Clock, iso

# Each entry: (version, SQL). Never edit an old entry once released;
# add a new one instead.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE kv_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            level   TEXT NOT NULL,
            kind    TEXT NOT NULL,
            message TEXT NOT NULL,
            data    TEXT
        );
        CREATE INDEX idx_events_ts ON events(ts);
        CREATE INDEX idx_events_kind ON events(kind);
        CREATE TABLE price_snapshots (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT NOT NULL,
            coin   TEXT NOT NULL,
            bid    REAL NOT NULL,
            ask    REAL NOT NULL,
            last   REAL NOT NULL,
            source TEXT NOT NULL
        );
        CREATE INDEX idx_price_snapshots_coin_ts ON price_snapshots(coin, ts);
        """,
    ),
    (
        2,
        """
        CREATE TABLE scanner_signals (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            cycle_id  TEXT,
            coin      TEXT NOT NULL,
            price_aud REAL,
            trend     TEXT,
            flagged   INTEGER NOT NULL,
            healthy   INTEGER NOT NULL,
            setups    TEXT NOT NULL,
            alerts    TEXT NOT NULL,
            data      TEXT NOT NULL
        );
        CREATE INDEX idx_scanner_signals_ts ON scanner_signals(ts);
        CREATE INDEX idx_scanner_signals_coin_ts ON scanner_signals(coin, ts);
        CREATE TABLE news_items (
            id           TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            title        TEXT NOT NULL,
            url          TEXT,
            summary      TEXT,
            published_at TEXT NOT NULL,
            fetched_at   TEXT NOT NULL,
            coins        TEXT NOT NULL,
            event_kinds  TEXT NOT NULL
        );
        CREATE INDEX idx_news_items_published ON news_items(published_at);
        CREATE TABLE news_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            coin        TEXT NOT NULL,
            kind        TEXT NOT NULL,
            summary     TEXT NOT NULL,
            source      TEXT NOT NULL,
            block_until TEXT NOT NULL
        );
        CREATE INDEX idx_news_events_until ON news_events(block_until);
        CREATE TABLE news_scores (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            cycle_id     TEXT,
            coin         TEXT NOT NULL,
            sentiment    REAL NOT NULL,
            reason       TEXT NOT NULL,
            major_event  INTEGER NOT NULL,
            event_summary TEXT,
            headlines_key TEXT NOT NULL,
            headline_count INTEGER NOT NULL,
            llm_call_id  INTEGER
        );
        CREATE INDEX idx_news_scores_coin_ts ON news_scores(coin, ts);
        CREATE TABLE llm_calls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            agent         TEXT NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            cache_read_tokens  INTEGER NOT NULL,
            cost_usd      REAL NOT NULL,
            latency_ms    INTEGER NOT NULL,
            ok            INTEGER NOT NULL,
            error         TEXT,
            request_id    TEXT
        );
        CREATE INDEX idx_llm_calls_ts ON llm_calls(ts);
        CREATE TABLE agent_outputs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            cycle_id    TEXT,
            agent       TEXT NOT NULL,
            coin        TEXT,
            ok          INTEGER NOT NULL,
            input       TEXT,
            output      TEXT,
            error       TEXT,
            llm_call_id INTEGER
        );
        CREATE INDEX idx_agent_outputs_ts ON agent_outputs(ts);
        """,
    ),
    (
        3,
        """
        CREATE TABLE debates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            cycle_id     TEXT,
            kind         TEXT NOT NULL,
            coin         TEXT NOT NULL,
            position_key TEXT,
            outcome      TEXT NOT NULL,
            proposal     TEXT,
            verdict      TEXT,
            problems     TEXT NOT NULL,
            transcript   TEXT NOT NULL
        );
        CREATE INDEX idx_debates_ts ON debates(ts);
        CREATE INDEX idx_debates_coin_ts ON debates(coin, ts);
        """,
    ),
    (
        4,
        """
        CREATE TABLE risk_decisions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            cycle_id     TEXT,
            kind         TEXT NOT NULL,
            coin         TEXT NOT NULL,
            approved     INTEGER NOT NULL,
            reason       TEXT,
            qty          REAL NOT NULL,
            price        REAL NOT NULL,
            notional_aud REAL NOT NULL,
            fee_aud      REAL NOT NULL,
            failures     TEXT NOT NULL,
            checks       TEXT NOT NULL,
            metrics      TEXT NOT NULL,
            debate_id    INTEGER
        );
        CREATE INDEX idx_risk_decisions_ts ON risk_decisions(ts);
        """,
    ),
    (
        5,
        """
        CREATE TABLE positions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            mode             TEXT NOT NULL,
            coin             TEXT NOT NULL,
            status           TEXT NOT NULL,
            qty              REAL NOT NULL,
            initial_qty      REAL NOT NULL,
            entry_price      REAL NOT NULL,
            entry_fee_aud    REAL NOT NULL,
            stop_loss        REAL NOT NULL,
            take_profit      REAL NOT NULL,
            highest_price    REAL NOT NULL,
            trailing_active  INTEGER NOT NULL DEFAULT 0,
            opened_at        TEXT NOT NULL,
            closed_at        TEXT,
            exit_price       REAL,
            exit_reason      TEXT,
            realized_pnl_aud REAL NOT NULL DEFAULT 0,
            cycle_id         TEXT,
            debate_id        INTEGER
        );
        CREATE INDEX idx_positions_status ON positions(mode, status);
        CREATE TABLE orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            mode          TEXT NOT NULL,
            coin          TEXT NOT NULL,
            side          TEXT NOT NULL,
            status        TEXT NOT NULL,
            reason        TEXT,
            requested_qty REAL NOT NULL,
            limit_price   REAL,
            filled_qty    REAL NOT NULL DEFAULT 0,
            avg_price     REAL,
            notional_aud  REAL NOT NULL DEFAULT 0,
            fee_aud       REAL NOT NULL DEFAULT 0,
            position_id   INTEGER,
            exchange_order_id TEXT,
            error         TEXT,
            detail        TEXT
        );
        CREATE INDEX idx_orders_status ON orders(status);
        CREATE TABLE trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            mode          TEXT NOT NULL,
            coin          TEXT NOT NULL,
            side          TEXT NOT NULL,
            qty           REAL NOT NULL,
            price         REAL NOT NULL,
            aud_amount    REAL NOT NULL,
            fee_aud       REAL NOT NULL,
            order_id      INTEGER NOT NULL,
            position_id   INTEGER NOT NULL,
            reason        TEXT,
            cost_base_aud REAL,
            proceeds_aud  REAL,
            gain_aud      REAL,
            acquired_at   TEXT
        );
        CREATE INDEX idx_trades_ts ON trades(mode, ts);
        CREATE TABLE equity_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  TEXT NOT NULL,
            mode                TEXT NOT NULL,
            equity_aud          REAL NOT NULL,
            cash_aud            REAL NOT NULL,
            positions_value_aud REAL NOT NULL,
            day_pnl_pct         REAL,
            open_positions      INTEGER NOT NULL
        );
        CREATE INDEX idx_equity_snapshots_ts ON equity_snapshots(mode, ts);
        """,
    ),
    (
        6,
        """
        ALTER TABLE positions ADD COLUMN trail_pct REAL;
        ALTER TABLE positions ADD COLUMN max_hold_hours REAL;
        ALTER TABLE positions ADD COLUMN strategy TEXT
        """,
    ),
    (
        7,
        """
        CREATE TABLE prop_accounts (
            account       TEXT PRIMARY KEY,
            broker        TEXT NOT NULL,
            size          REAL NOT NULL,
            phase         TEXT NOT NULL,
            day_ref       REAL,
            day_ref_at    TEXT,
            halted_until  TEXT,
            target_hit_at TEXT,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE prop_positions (
            account     TEXT NOT NULL,
            coin        TEXT NOT NULL,
            side        INTEGER NOT NULL,
            qty         REAL NOT NULL,
            entry_price REAL NOT NULL,
            opened_at   TEXT NOT NULL,
            stop        REAL NOT NULL,
            trail_dist  REAL,
            best        REAL NOT NULL,
            stop_ref    TEXT,
            PRIMARY KEY (account, coin)
        );
        CREATE TABLE prop_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account     TEXT NOT NULL,
            coin        TEXT NOT NULL,
            side        INTEGER NOT NULL,
            qty         REAL NOT NULL,
            entry_price REAL NOT NULL,
            exit_price  REAL,
            opened_at   TEXT NOT NULL,
            closed_at   TEXT NOT NULL,
            pnl         REAL,
            reason      TEXT NOT NULL
        );
        CREATE INDEX idx_prop_trades_closed ON prop_trades(account, closed_at);
        CREATE TABLE prop_equity (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            ts      TEXT NOT NULL,
            balance REAL NOT NULL,
            equity  REAL NOT NULL
        );
        CREATE INDEX idx_prop_equity_ts ON prop_equity(account, ts);
        CREATE TABLE prop_reviews (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,
            coin       TEXT NOT NULL,
            mode       TEXT NOT NULL,
            verdict    TEXT NOT NULL,
            confidence INTEGER,
            reason     TEXT,
            blocked    INTEGER NOT NULL
        );
        CREATE TABLE paper_prop_accounts (
            account TEXT PRIMARY KEY,
            balance REAL NOT NULL
        );
        CREATE TABLE paper_prop_positions (
            account     TEXT NOT NULL,
            coin        TEXT NOT NULL,
            side        INTEGER NOT NULL,
            qty         REAL NOT NULL,
            entry_price REAL NOT NULL,
            opened_at   TEXT NOT NULL,
            stop        REAL NOT NULL,
            fees        REAL NOT NULL,
            swap        REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (account, coin)
        )
        """,
    ),
]


class Database:
    def __init__(self, path: str | Path, clock: Clock | None = None):
        self.path = Path(path)
        self.clock = clock or Clock()
        self._local = threading.local()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- connections ---------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # autocommit; use tx() for transactions
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            # FULL: every committed write reaches the disk before we carry on, so a trade
            # recorded a split second before a power cut is never lost. (Cheap at our write rate.)
            c.execute("PRAGMA synchronous=FULL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def tx(self) -> _Transaction:
        """`with db.tx() as c:` runs several statements all-or-nothing."""
        return _Transaction(self.conn())

    # -- schema --------------------------------------------------------
    def migrate(self) -> int:
        c = self.conn()
        current = c.execute("PRAGMA user_version").fetchone()[0]
        for version, sql in MIGRATIONS:
            if version > current:
                with self.tx() as t:
                    for statement in _split_sql(sql):
                        t.execute(statement)
                    t.execute(f"PRAGMA user_version = {version}")
                current = version
        return current

    def schema_version(self) -> int:
        return self.conn().execute("PRAGMA user_version").fetchone()[0]

    # -- small helpers ---------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv_state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, iso(self.clock.now())),
        )

    def log_event(self, kind: str, message: str, *, level: str = "INFO", data: Any = None) -> int:
        cur = self.execute(
            "INSERT INTO events(ts, level, kind, message, data) VALUES (?, ?, ?, ?, ?)",
            (iso(self.clock.now()), level, kind, message, _to_json(data)),
        )
        return int(cur.lastrowid)

    def insert_price_snapshots(self, rows: Iterable[tuple[str, float, float, float]], source: str) -> None:
        ts = iso(self.clock.now())
        with self.tx() as t:
            t.executemany(
                "INSERT INTO price_snapshots(ts, coin, bid, ask, last, source) VALUES (?, ?, ?, ?, ?, ?)",
                [(ts, coin, bid, ask, last, source) for coin, bid, ask, last in rows],
            )


class _Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    def __enter__(self) -> sqlite3.Connection:
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, exc, tb) -> None:
        self.c.execute("ROLLBACK" if exc_type else "COMMIT")


def _to_json(data: Any) -> str | None:
    if data is None:
        return None
    return json.dumps(data, default=str, sort_keys=True)


def _split_sql(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
