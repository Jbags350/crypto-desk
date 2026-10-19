import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from desk.clock import Clock
from desk.main import EXIT_HUNG, Runner, next_cycle_time
from desk.notify import MemoryNotifier


@pytest.mark.parametrize("now,expected", [
    ("08:00:00", "08:00:20"),
    ("08:00:20", "08:15:20"),      # exactly on the slot: next one
    ("08:07:41", "08:15:20"),
    ("08:59:59", "09:00:20"),
    ("23:50:00", "00:00:20"),      # rolls over midnight
])
def test_cycles_align_to_candle_closes(now, expected):
    h, m, s = map(int, now.split(":"))
    t = next_cycle_time(datetime(2026, 10, 9, h, m, s, tzinfo=UTC), 15, 20)
    assert t.strftime("%H:%M:%S") == expected


class FakeDesk:
    def __init__(self, db, fail_fast=0):
        self.db = db
        self.notify = MemoryNotifier()
        self.fast_calls = 0
        self.cycle_calls = 0
        self.fail_fast = fail_fast
        self.heartbeats = {}

    def fast_tick(self):
        self.fast_calls += 1
        if self.fast_calls <= self.fail_fast:
            raise RuntimeError("boom")
        self.heartbeats["fast"] = datetime.now(UTC)

    def agent_cycle(self):
        self.cycle_calls += 1
        self.heartbeats["cycle"] = datetime.now(UTC)

    def last_heartbeat(self, which):
        return self.heartbeats.get(which)


def test_loop_errors_are_logged_and_retried(db):
    desk = FakeDesk(db, fail_fast=3)
    r = Runner(desk, Clock(), 60, 15, 20)
    for _ in range(4):
        r._guarded("fast", desk.fast_tick)
    assert desk.fast_calls == 4 and r.errors["fast"] == 0          # recovered on the 4th try
    assert db.query_one("SELECT COUNT(*) FROM events WHERE kind = 'error'")[0] == 3
    assert [k for k, _ in desk.notify.sent] == ["error"]           # alerted once, on the 3rd failure


def test_runner_runs_both_loops_and_stops_cleanly(db):
    desk = FakeDesk(db)
    r = Runner(desk, Clock(), fast_seconds=1, cycle_minutes=15, delay_seconds=20, watch_seconds=0.05)
    threading.Timer(1.5, r.stop.set).start()
    assert r.run_forever() == 0
    assert desk.fast_calls >= 2 and desk.cycle_calls == 1           # one cycle straight away at start-up


def test_watchdog_exits_when_a_loop_hangs(db):
    desk = FakeDesk(db)
    desk.fast_tick = lambda: time.sleep(5)                          # hung
    desk.heartbeats["fast"] = datetime.now(UTC) - timedelta(minutes=30)
    exits = []

    def fake_exit(code):
        exits.append(code)
        r.stop.set()

    r = Runner(desk, Clock(), fast_seconds=60, cycle_minutes=15, delay_seconds=20, exit_fn=fake_exit,
               watch_seconds=0.05)
    r.run_forever()
    assert exits and exits[0] == EXIT_HUNG
    assert any("restarting the bot" in t for _, t in desk.notify.sent)


# ---- single instance + keep awake --------------------------------------------------

def test_only_one_copy_can_hold_the_lock(tmp_path):
    from desk.system import InstanceLock
    a, b = InstanceLock(tmp_path / "desk.lock"), InstanceLock(tmp_path / "desk.lock")
    assert a.acquire()
    assert not b.acquire()                 # a second bot on the same database is refused
    a.release()
    assert b.acquire()                     # free again once the first one stops
    b.release()


def test_different_databases_dont_block_each_other(tmp_path):
    from desk.system import InstanceLock
    a, b = InstanceLock(tmp_path / "one.lock"), InstanceLock(tmp_path / "two.lock")
    assert a.acquire() and b.acquire()
    a.release()
    b.release()


def test_keep_awake_can_be_switched_on_and_off():
    import os
    from desk.system import keep_awake
    if os.name == "nt":
        assert keep_awake(True) is True
        assert keep_awake(False) is True
    else:
        assert keep_awake(True) is False
