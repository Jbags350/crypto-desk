import json
import threading

import pytest

from desk.clock import Clock
from desk.db import MIGRATIONS, Database


def test_migrate_is_idempotent(db):
    v = db.schema_version()
    assert v == MIGRATIONS[-1][0]
    assert db.migrate() == v


def test_wal_mode(db):
    assert db.query_one("PRAGMA journal_mode")[0].lower() == "wal"


def test_writes_are_power_cut_safe(db):
    assert db.query_one("PRAGMA synchronous")[0] == 2      # 2 = FULL: flushed to disk on every commit


def test_kv_roundtrip(db):
    assert db.kv_get("x") is None
    assert db.kv_get("x", "d") == "d"
    db.kv_set("x", "1")
    db.kv_set("x", "2")
    assert db.kv_get("x") == "2"


def test_log_event(db):
    db.log_event("check", "hello", data={"a": 1})
    row = db.query_one("SELECT * FROM events")
    assert row["kind"] == "check" and json.loads(row["data"]) == {"a": 1}
    assert row["ts"].startswith("2026-10-09T08:00:00")


def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO kv_state VALUES ('a', '1', 'now')")
            raise RuntimeError("boom")
    assert db.kv_get("a") is None


def test_price_snapshots(db):
    db.insert_price_snapshots([("BTC", 1.0, 2.0, 1.5), ("ETH", 3.0, 4.0, 3.5)], source="coinspot")
    assert len(db.query("SELECT * FROM price_snapshots")) == 2


def test_each_thread_gets_its_own_connection(tmp_path):
    d = Database(tmp_path / "t.db", Clock())
    d.migrate()
    errors = []

    def worker(i):
        try:
            for j in range(20):
                d.kv_set(f"k{i}", str(j))
        except Exception as exc:   # pragma: no cover
            errors.append(exc)
        finally:
            d.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert d.kv_get("k3") == "19"
