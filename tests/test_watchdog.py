"""Stage 8: watchdog, health check, daily clean-up, clean shutdown, Docker files."""

import ast
import signal
import sqlite3
import threading
from datetime import timedelta

import httpx
import pytest
import yaml

from desk import healthcheck, housekeeping
from desk.clock import Clock, iso
from desk.config import PROJECT_ROOT, Secrets
from desk.main import K_STOPPED, Runner, stop_on_sigterm
from desk.telegram import MultiNotifier, TelegramError
from desk.watchdog import K_DOWN_SINCE, Watchdog
from tests.test_main import FakeDesk


class FakeHttp:
    def __init__(self, fail=False):
        self.urls = []
        self.fail = fail

    def get(self, url):
        self.urls.append(url)
        if self.fail:
            raise httpx.ConnectError("no internet")


def beat(db, clock, minutes_ago=0.0):
    db.kv_set("heartbeat_fast", iso(clock.now() - timedelta(minutes=minutes_ago)))


@pytest.fixture
def sent():
    return []


@pytest.fixture
def dog(config, db, clock, sent):
    return Watchdog(config, Secrets(), db, clock, send=sent.append, http=FakeHttp())


def test_healthy_bot_means_no_messages(dog, db, clock, sent):
    beat(db, clock, minutes_ago=1)
    assert dog.check() == "ok"
    assert sent == []


def test_alerts_after_10_minutes_repeats_hourly_then_says_recovered(dog, db, clock, sent):
    beat(db, clock)
    clock.advance(minutes=11)                       # past the start-up grace period too
    assert dog.check() == "down"
    assert len(sent) == 1 and "stopped working" in sent[0] and "11 minutes ago" in sent[0]

    clock.advance(minutes=30)
    assert dog.check() == "down" and len(sent) == 1  # no spam: reminders are hourly
    clock.advance(minutes=31)
    assert dog.check() == "down" and len(sent) == 2

    beat(db, clock)                                 # the bot is back
    assert dog.check() == "ok"
    assert "running again" in sent[2] and "72 minutes" in sent[2]
    assert db.kv_get(K_DOWN_SINCE) == ""
    clock.advance(minutes=1)
    beat(db, clock)
    dog.check()
    assert len(sent) == 3                            # "recovered" is said once


def test_no_alert_just_under_the_limit(dog, db, clock, sent):
    beat(db, clock)
    clock.advance(minutes=10)                        # exactly 10 minutes: still fine
    assert dog.check() == "ok" and sent == []


def test_quick_restarts_are_silent(dog, db, clock, sent):
    """paper.bat / Docker restart a crashed bot within seconds: nothing to report."""
    beat(db, clock)
    for _ in range(30):
        clock.advance(minutes=2)
        beat(db, clock)
        dog.check()
    assert sent == []


def test_grace_period_when_both_start_together(dog, db, clock, sent):
    """Docker starts the bot and watchdog at once; an empty database isn't an emergency yet."""
    assert dog.check() == "starting"
    clock.advance(minutes=5)
    assert dog.check() == "starting" and sent == []
    clock.advance(minutes=6)
    assert dog.check() == "down"
    assert "never" in sent[0]


def test_a_known_outage_is_reported_straight_after_a_watchdog_restart(config, db, clock, sent):
    beat(db, clock, minutes_ago=90)
    db.kv_set(K_DOWN_SINCE, iso(clock.now() - timedelta(minutes=90)))   # remembered from before
    dog = Watchdog(config, Secrets(), db, clock, send=sent.append, http=FakeHttp())
    assert dog.check() == "down" and len(sent) == 1


def test_clean_stop_gets_one_calm_message(dog, db, clock, sent):
    beat(db, clock)
    db.kv_set(K_STOPPED, iso(clock.now() + timedelta(seconds=5)))       # stopped after its last heartbeat
    clock.advance(minutes=15)
    assert dog.check() == "stopped"
    assert len(sent) == 1 and "stopped by you" in sent[0]
    clock.advance(hours=5)
    assert dog.check() == "stopped" and len(sent) == 1                  # no repeated alarms

    beat(db, clock)                                                     # started again
    assert dog.check() == "ok"
    assert "running again" in sent[1]


def test_old_clean_stop_doesnt_hide_a_later_crash(dog, db, clock, sent):
    db.kv_set(K_STOPPED, iso(clock.now() - timedelta(days=1)))          # stopped yesterday, then restarted
    beat(db, clock)
    clock.advance(minutes=12)                                           # now it's crashed
    assert dog.check() == "down"
    assert "stopped working" in sent[0]


def test_telegram_failure_doesnt_crash_the_watchdog(config, db, clock):
    def broken(text):
        raise TelegramError("Telegram is down")

    dog = Watchdog(config, Secrets(), db, clock, send=broken, http=FakeHttp())
    beat(db, clock)
    clock.advance(minutes=11)
    assert dog.check() == "down"
    assert db.query_one("SELECT COUNT(*) FROM events WHERE kind = 'watchdog'")[0] == 1


def test_telegram_set_up_later_is_picked_up(config, db, clock, monkeypatch):
    sent = []

    class FakeTg:
        def __init__(self, token):
            pass

        def send(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr("desk.watchdog.TelegramClient", FakeTg)
    later = Secrets(telegram_bot_token="123:abc", telegram_chat_id="42")
    dog = Watchdog(config, Secrets(), db, clock, http=FakeHttp(), reload_secrets=lambda: later)
    assert dog._send is None
    beat(db, clock)
    clock.advance(minutes=11)
    dog.check()
    assert sent and sent[0][0] == "42"


def test_healthchecks_io_pings(config, db, clock, sent):
    http = FakeHttp()
    dog = Watchdog(config, Secrets(healthchecks_url="https://hc-ping.com/uuid"), db, clock,
                   send=sent.append, http=http)
    beat(db, clock)
    dog.check()
    clock.advance(minutes=11)
    dog.check()
    assert http.urls == ["https://hc-ping.com/uuid", "https://hc-ping.com/uuid/fail"]


def test_ping_failure_is_harmless(config, db, clock, sent):
    dog = Watchdog(config, Secrets(healthchecks_url="https://hc-ping.com/uuid"), db, clock,
                   send=sent.append, http=FakeHttp(fail=True))
    beat(db, clock)
    assert dog.check() == "ok"


def test_no_healthchecks_url_means_no_pings(dog, db, clock):
    beat(db, clock)
    dog.check()
    assert dog.http.urls == []


def test_run_forever_survives_a_failing_check(dog, monkeypatch):
    calls = []

    def bad_check():
        calls.append(1)
        raise RuntimeError("database locked")

    def fake_sleep(seconds):
        if len(calls) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(dog, "check", bad_check)
    with pytest.raises(KeyboardInterrupt):
        dog.run_forever(sleep=fake_sleep)
    assert len(calls) == 3


# ---- health check (Docker) ---------------------------------------------------------

def test_health_check_reads_the_heartbeat(config, db, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(type(config), "path", lambda self, name: tmp_path / "test.db")
    assert healthcheck.check(config, clock) == (False, "the bot has never run on this database")
    beat(db, clock, minutes_ago=1)
    ok, why = healthcheck.check(config, clock)
    assert ok and "60s ago" in why
    clock.advance(minutes=6)                     # 7 minutes: over max(5 x 60s, 300s)
    ok, why = healthcheck.check(config, clock)
    assert not ok and "7 minutes" in why


def test_health_check_never_creates_a_database(config, clock, tmp_path, monkeypatch):
    missing = tmp_path / "nothing.db"
    monkeypatch.setattr(type(config), "path", lambda self, name: missing)
    ok, why = healthcheck.check(config, clock)
    assert not ok and "no database yet" in why
    assert not missing.exists()


def test_health_check_connection_is_read_only(db, clock, tmp_path):
    beat(db, clock)
    assert healthcheck.read_heartbeat(tmp_path / "test.db") == db.kv_get("heartbeat_fast")
    uri = f"{(tmp_path / 'test.db').resolve().as_uri()}?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("DELETE FROM kv_state")
    c.close()


def test_health_check_main_reports_problems_as_unhealthy(monkeypatch, capsys):
    def broken():
        raise RuntimeError("config gone")

    monkeypatch.setattr(healthcheck, "load_config", broken)
    assert healthcheck.main() == 1
    assert "unhealthy" in capsys.readouterr().out


# ---- daily clean-up -----------------------------------------------------------------

def _rows(db, table):
    return db.query_one(f"SELECT COUNT(*) FROM {table}")[0]


def test_housekeeping_deletes_only_old_rows(db, clock):
    now = clock.now()
    for days in (1, 13, 15, 40):
        ts = iso(now - timedelta(days=days))
        db.execute("INSERT INTO scanner_signals(ts, coin, flagged, healthy, setups, alerts, data) "
                   "VALUES (?, 'BTC', 0, 1, '[]', '[]', '{}')", (ts,))
    for days in (29, 31):
        db.execute("INSERT INTO price_snapshots(ts, coin, bid, ask, last, source) VALUES (?, 'BTC', 1, 1, 1, 't')",
                   (iso(now - timedelta(days=days)),))
    removed = housekeeping.prune(db, clock)
    assert removed["scanner_signals"] == 2 and removed["price_snapshots"] == 1
    assert _rows(db, "scanner_signals") == 2 and _rows(db, "price_snapshots") == 1


def test_housekeeping_runs_once_a_day_and_keeps_records(db, clock):
    db.log_event("test", "keep me")
    assert housekeeping.run_daily(db, clock) is not None
    assert housekeeping.run_daily(db, clock) is None              # already done today
    clock.advance(days=1)
    assert housekeeping.run_daily(db, clock) is not None          # tomorrow again
    assert _rows(db, "events") >= 1


def test_housekeeping_never_touches_trading_records():
    assert not {"trades", "positions", "orders", "debates", "risk_decisions", "llm_calls",
                "equity_snapshots", "events"} & (set(housekeeping.RETENTION_DAYS) | {"news_items"})


# ---- clean shutdown ---------------------------------------------------------------

def test_clean_shutdown_leaves_a_note_for_the_watchdog(db, clock):
    desk = FakeDesk(db)
    desk.notify = MultiNotifier(desk.notify)
    beat(db, clock)
    r = Runner(desk, clock, fast_seconds=60, cycle_minutes=15, delay_seconds=20, watch_seconds=0.05)
    r.shutdown([])
    assert r.stop.is_set()
    assert db.kv_get(K_STOPPED) == iso(clock.now())
    assert db.query_one("SELECT COUNT(*) FROM events WHERE kind = 'shutdown'")[0] == 1


def test_ctrl_c_runs_the_clean_shutdown(db):
    desk = FakeDesk(db)
    r = Runner(desk, Clock(), fast_seconds=60, cycle_minutes=15, delay_seconds=20, watch_seconds=0.05)

    real_wait = r.stop.wait

    def wait(timeout=None):
        if threading.current_thread() is threading.main_thread():
            raise KeyboardInterrupt             # the watch loop (main thread) "gets Ctrl+C"
        return real_wait(timeout)

    r.stop.wait = wait
    assert r.run_forever() == 0
    assert db.kv_get(K_STOPPED)


def test_sigterm_is_treated_like_ctrl_c():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers can only be set from the main thread")
    old = signal.getsignal(signal.SIGTERM)
    try:
        stop_on_sigterm()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, old)


# ---- Docker files -------------------------------------------------------------------

def test_compose_file_is_safe_and_restarts_the_bot():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"bot", "watchdog", "dashboard"}
    for name, svc in services.items():
        assert svc["restart"] == "unless-stopped", name
        assert svc["env_file"] == ".env", name
        assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "5"}, name
        assert "./data:/app/data" in svc["volumes"], name
        assert "environment" not in svc, name                # secrets only come from .env
    assert services["bot"]["healthcheck"]["test"][-1] == "desk.healthcheck"
    # one image, built once (building it from three services at once fails)
    assert [n for n, svc in services.items() if "build" in svc] == ["bot"]
    assert {svc["image"] for svc in services.values()} == {"crypto-desk:latest"}
    # the dashboard must only be reachable from the server itself
    assert services["dashboard"]["ports"] == ["127.0.0.1:8501:8501"]
    assert "ports" not in services["bot"] and "ports" not in services["watchdog"]


def test_secrets_never_go_into_the_image():
    ignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore and "data/" in ignore
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert ".env" not in dockerfile
    assert "USER root" not in dockerfile


def test_container_entrypoint_switches_to_a_normal_user():
    src = (PROJECT_ROOT / "docker" / "entrypoint.py").read_text(encoding="utf-8")
    ast.parse(src)                               # valid Python (it can only run on Linux)
    assert "os.setuid" in src and "os.setgid" in src and "os.setgroups" in src
    assert '"desk"' in src
    assert 'ENTRYPOINT ["python", "/app/docker/entrypoint.py"]' in (PROJECT_ROOT / "Dockerfile").read_text()


def test_watchdog_config_is_sensible(config):
    w = config.watchdog
    assert w.alert_after_minutes == 10
    assert w.repeat_alert_minutes >= w.alert_after_minutes
    assert w.check_every_seconds <= 60
