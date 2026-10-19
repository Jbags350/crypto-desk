"""Dashboard data + the Streamlit page itself (run headless with Streamlit's AppTest)."""

from pathlib import Path

import pytest
import yaml

from desk import dashboard_data as dd
from desk.broker.paper import PaperBroker
from desk.config import DEFAULT_CONFIG_PATH
from desk.controls import ControlPanel
from desk.executor import Executor
from desk.notify import MemoryNotifier
from desk.portfolio import Portfolio
from desk.risk import RiskDecision
from desk.status import positions_text, status_text
from tests.fakes import FakeCoinSpot

APP = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"


def filled_db(config, db, clock):
    cs = FakeCoinSpot(clock, {"BTC": 100_000.0, "ETH": 4_000.0})
    pf = Portfolio(db, clock, config)
    ex = Executor(db, clock, config, PaperBroker(cs, config), pf, MemoryNotifier())
    d = RiskDecision(kind="entry", coin="BTC", approved=True, checks=(), qty=0.004, price=100_050.0,
                     max_price=100_600.0, stop_loss_aud=98_000.0, take_profit_aud=104_000.0)
    pos = ex.open_position(d, trail_pct=5.0, strategy="trend")
    eth = ex.open_position(RiskDecision(kind="entry", coin="ETH", approved=True, checks=(), qty=0.1, price=4_002.0,
                                        max_price=4_030.0, stop_loss_aud=3_900.0, take_profit_aud=4_200.0))
    cs.mid["ETH"] = 4_150.0
    ex.close_position(eth, eth.qty, "take_profit")
    pf.snapshot_equity(cs.latest_all())
    clock.advance(minutes=1)
    pf.snapshot_equity(cs.latest_all())
    db.execute("INSERT INTO debates(ts, kind, coin, outcome, proposal, verdict, problems, transcript) VALUES "
               "(?, 'entry', 'BTC', 'approved', ?, ?, '[]', '[]')",
               (clock.now().isoformat(), '{"decision": "BUY", "confidence": 70, "entry_aud": 1, "stop_loss_aud": 0.9,'
                ' "take_profit_aud": 2, "reasoning": "clean breakout"}',
                '{"vote": "BUY", "arguments_against": ["could fade"], "reasoning": "ok"}'))
    return cs, pos


def test_dashboard_queries(config, db, clock):
    cs, pos = filled_db(config, db, clock)
    k = dd.kpis(db, clock, config)
    assert k["open_positions"] == 1 and k["trades_today"] == 3
    assert len(dd.equity_series(db)) == 2
    p = dd.positions_df(db, clock, config, cs.latest_all())
    assert list(p["Coin"]) == ["BTC"] and p.iloc[0]["Trailing"] == "on"
    assert p.iloc[0]["Target (A$)"] == pytest.approx(104_000.0)        # a real target is shown
    assert p.iloc[0]["P&L A$"] is not None
    t = dd.trades_df(db, clock)
    assert list(t["Side"]) == ["SELL", "BUY", "BUY"] and t.iloc[0]["P&L (A$)"] > 0
    assert dd.debates(db, clock)[0]["verdict"]["vote"] == "BUY"
    assert not dd.events_df(db, clock).empty


def test_status_and_positions_text(config, db, clock):
    cs, _ = filled_db(config, db, clock)
    s = status_text(db, clock, config, compact=True)
    assert "Equity A$" in s and "Open positions: 1 (BTC)" in s and "1 sell(s)" in s
    full = status_text(db, clock, config)
    assert "TODAY'S TRADES" in full and "RECENT EVENTS" in full
    p = positions_text(db, clock, config, cs.latest_all())
    assert p.startswith("BTC:") and "trailing stop" in p
    assert "unavailable" in positions_text(db, clock, config, None)


@pytest.fixture
def app_env(tmp_path, monkeypatch, config):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["paths"]["database"] = str(tmp_path / "dash.db")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DESK_CONFIG", str(cfg))
    monkeypatch.setenv("DESK_ENV", str(env))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    return tmp_path, env


def run_app():
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.cache_resource.clear()        # the app caches its config/DB; each test has its own
    st.cache_data.clear()
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    return at


def test_dashboard_page_renders(app_env):
    at = run_app()
    assert not at.exception, at.exception
    assert "Crypto Desk" in at.title[0].value
    assert any("Pause" in b.label for b in at.button)


def test_dashboard_pause_button_flips_the_switch(app_env):
    from desk.clock import Clock
    from desk.db import Database
    tmp, _ = app_env
    at = run_app()
    next(b for b in at.button if "Pause" in b.label).click().run()
    assert not at.exception
    db = Database(tmp / "dash.db", Clock())
    db.migrate()
    assert ControlPanel(db, Clock()).state().paused


def test_dashboard_password(app_env):
    tmp, env = app_env
    env.write_text("DASHBOARD_PASSWORD=letmein123\n", encoding="utf-8")
    at = run_app()
    assert not at.exception
    assert len(at.text_input) == 1 and not at.button                # nothing else until the password is right
    at.text_input[0].input("wrong").run()
    assert at.error
    at.text_input[0].input("letmein123").run()
    assert any("Pause" in b.label for b in at.button)
