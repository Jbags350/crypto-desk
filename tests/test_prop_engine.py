"""The prop engine on a paper (simulated Tradeify 247) account, with prices and signals under test control."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from desk.clock import FakeClock, iso
from desk.controls import ControlPanel
from desk.notify import MemoryNotifier
from desk.prop.brokers import PaperPropBroker
from desk.prop.engine import PropEngine, last_boundary
from desk.prop.live_signals import CoinView, LiveSignals, RotationDecision
from desk.prop.review import PropReviewer
from desk.prop.rules import tradeify_2step
from desk.schemas import PropReview
from tests.fakes import baseline_config


class FakePrices:
    def __init__(self, **mids):
        self.m = dict(mids)

    def mids(self):
        return dict(self.m)


class FakeSignals:
    def __init__(self):
        self.decision = None
        self.closes = (None, {})

    def rotation(self, now):
        return self.decision

    def last_closes(self, now):
        return self.closes


def decision(at: datetime, buys: dict, sell=(), bull=True) -> RotationDecision:
    """buys: coin -> (price, stop_pct)."""
    views = {c: CoinView(c, p, 0.2, i + 1, True, 10.0, 4.0, 1.0) for i, (c, (p, _)) in enumerate(buys.items())}
    return RotationDecision(at, bull, {c: 10.0 - i for i, c in enumerate(buys)}, set(sell),
                            {c: p * s for c, (p, s) in buys.items()}, views)


START = datetime(2026, 10, 11, 0, 2, tzinfo=UTC)        # 2 minutes after the 00:00 UTC rebalance candle


@pytest.fixture
def env(db):
    clock = FakeClock(START, tz="Australia/Brisbane")
    db.clock = clock
    cfg = baseline_config()
    cfg = cfg.model_copy(update={"prop": cfg.prop.model_copy(update={"ai_review": "off", "enabled": True})})
    rules = tradeify_2step(100_000)
    prices = FakePrices(BTC=100_000.0, ETH=4_000.0, SOL=200.0, XRP=2.0)
    broker = PaperPropBroker(db, clock, rules, prices, ["paper-1"], cache_seconds=0)
    sig = FakeSignals()
    notify = MemoryNotifier()
    sleeps = []
    eng = PropEngine(cfg, rules, broker, sig, db, clock, notify, ControlPanel(db, clock), sleep=sleeps.append)
    eng.startup()

    class E:
        pass

    e = E()
    e.clock, e.db, e.cfg, e.rules, e.prices, e.broker, e.sig, e.notify, e.eng, e.sleeps = (
        clock, db, cfg, rules, prices, broker, sig, notify, eng, sleeps)
    return e


def kinds(e):
    return [k for k, _ in e.notify.sent]


def test_rebalance_buys_with_risk_based_size(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05), "SOL": (200.0, 0.08)})
    e.eng.minute()
    notes = e.eng.notes("paper-1")
    assert set(notes) == {"ETH", "SOL"}
    # 1% of 100k = $1,000 risk; ETH stop 5% away at $4,000 -> 5 ETH ($20,000)
    assert notes["ETH"].qty == pytest.approx(5.0)
    half = e.rules.half_cost_pct("ETH") / 100
    assert notes["ETH"].entry_price == pytest.approx(4_000 * (1 + half))
    assert notes["ETH"].stop == pytest.approx(notes["ETH"].entry_price * 0.95)
    # SOL: $1,000 / (8% of $200) = 62.5 SOL, rounded down to the 0.01 step
    assert notes["SOL"].qty == pytest.approx(62.5)
    assert kinds(e).count("trade") == 2
    assert e.db.kv_get("prop_last_rebalance") == iso(START.replace(minute=0))


def test_leverage_caps_the_size(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"XRP": (2.0, 0.002)})     # tiny stop -> huge size wanted
    e.eng.minute()
    n = e.eng.notes("paper-1")["XRP"]
    assert n.qty * 2.0 <= 100_000 * 0.9 * 2 + 1                                   # 2:1 leverage, 90% margin use


def test_rotation_sells_coins_that_dropped_out(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    e.clock.advance(days=1)
    e.sig.decision = decision(START.replace(minute=0) + timedelta(days=1), {}, sell={"ETH"})
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}
    trade = e.db.query_one("SELECT * FROM prop_trades")
    assert trade["coin"] == "ETH" and trade["reason"] == "rotation"


def test_late_rebalance_is_skipped(env):
    e = env
    e.clock.set(START + timedelta(hours=4))
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}
    assert "error" in kinds(e)


def test_stop_order_fills_when_price_falls(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    stop = e.eng.notes("paper-1")["ETH"].stop
    e.prices.m["ETH"] = stop * 0.99
    e.clock.advance(minutes=1)
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}
    t = e.db.query_one("SELECT * FROM prop_trades")
    assert t["reason"] == "stop" and t["pnl"] < -1000          # about -1R, a bit more after the gap and costs
    assert any("stopped out" in m for _, m in e.notify.sent)


def test_daily_brake_closes_everything_and_rests_until_22_utc(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.10)})   # 2.5 ETH, wide stop
    e.eng.minute()
    e.broker.move_stop("paper-1", "ETH", 1.0, None)          # keep the stop out of the way for this test
    e.prices.m["ETH"] = 3_150.0                             # -21% on 2.5 ETH: about -2.1% of the account
    e.clock.advance(minutes=1)
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}
    row = e.db.query_one("SELECT * FROM prop_accounts")
    assert row["halted_until"] == iso(datetime(2026, 10, 11, 22, 0, tzinfo=UTC))
    assert "risk_limit" in kinds(e)
    # still resting at the next rebalance? (none today) -> after the 22:00 reset it trades again
    e.clock.set(datetime(2026, 10, 11, 22, 1, tzinfo=UTC))
    e.eng.minute()
    assert e.db.query_one("SELECT halted_until FROM prop_accounts")[0] is None


def test_halted_account_does_not_buy(env):
    e = env
    e.db.execute("UPDATE prop_accounts SET halted_until = ?", (iso(START + timedelta(hours=10)),))
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}


def test_phase_target_closes_everything_and_stops_trading(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.02)})   # 12.5 ETH = $50k position
    e.eng.minute()
    e.prices.m["ETH"] = 4_000 * 1.25                         # +$12.5k > +10%
    e.clock.advance(minutes=1)
    e.eng.minute()
    row = e.db.query_one("SELECT * FROM prop_accounts")
    assert row["target_hit_at"] is not None
    assert e.eng.notes("paper-1") == {}
    assert any("target reached" in m for _, m in e.notify.sent)
    # the next day's rotation leaves the account alone
    e.clock.advance(days=1)
    e.sig.decision = decision(START.replace(minute=0) + timedelta(days=1), {"SOL": (200.0, 0.05)})
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}


def test_trailing_stop_only_moves_up(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    n0 = e.eng.notes("paper-1")["ETH"]
    e.clock.advance(hours=1)
    e.prices.m["ETH"] = 4_400.0
    e.sig.closes = (START.replace(minute=0) + timedelta(hours=1), {"ETH": 4_400.0})
    e.eng.minute()
    n1 = e.eng.notes("paper-1")["ETH"]
    assert n1.best == 4_400.0 and n1.stop == pytest.approx(4_400.0 - n0.trail_dist)   # about 4,200
    e.clock.advance(hours=1)
    e.prices.m["ETH"] = 4_300.0                               # dips, but stays above the raised stop
    e.sig.closes = (START.replace(minute=0) + timedelta(hours=2), {"ETH": 4_300.0})
    e.eng.minute()
    assert e.eng.notes("paper-1")["ETH"].stop == pytest.approx(n1.stop)       # never moves down
    assert e.db.query_one("SELECT COUNT(*) FROM prop_equity")[0] == 2


def test_smaller_trades_near_the_floor(env):
    e = env
    assert e.eng.risk_for(100_000) == pytest.approx(1.0)
    assert e.eng.risk_for(97_000) == pytest.approx(0.5)      # half the 6% cushion left
    assert e.eng.risk_for(94_500) == pytest.approx(0.25)     # never below a quarter


def test_pause_blocks_buys_but_not_sells(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    ControlPanel(e.db, e.clock).pause(by="test")
    e.clock.advance(days=1)
    e.sig.decision = decision(START.replace(minute=0) + timedelta(days=1), {"SOL": (200.0, 0.05)}, sell={"ETH"})
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}                      # ETH sold, SOL not bought


def test_kill_close_flattens(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    ControlPanel(e.db, e.clock).kill(by="test", close_all=True)
    e.clock.advance(minutes=1)
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}
    assert e.db.query_one("SELECT reason FROM prop_trades")[0] == "kill_close"


def test_min_hold_is_respected(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    ControlPanel(e.db, e.clock).request_close_all(by="test")
    e.eng.minute()                                           # same instant: must wait ~21 s first
    assert e.sleeps and e.sleeps[0] == pytest.approx(21.0)


def test_swap_and_daily_reference_at_22_utc(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    bal_before = e.db.query_one("SELECT balance FROM paper_prop_accounts")[0]
    e.clock.set(datetime(2026, 10, 11, 22, 0, 30, tzinfo=UTC))
    e.eng.minute()
    bal_after = e.db.query_one("SELECT balance FROM paper_prop_accounts")[0]
    assert bal_before - bal_after == pytest.approx(5 * 4_000 * 0.00033, rel=1e-6)
    assert "daily_summary" in kinds(e)
    e.eng.minute()
    assert kinds(e).count("daily_summary") == 1             # once per day


def test_positions_closed_while_away_are_recorded(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    e.db.execute("DELETE FROM paper_prop_positions")         # gone at the broker (stop filled while we were off)
    warnings = e.eng.startup()
    assert any("closed while the bot was off" in w for w in warnings)
    assert e.eng.notes("paper-1") == {}


def test_heartbeat_for_the_watchdog(env):
    env.eng.minute()
    assert env.db.kv_get("heartbeat_fast") == iso(START)


def test_last_boundary():
    assert last_boundary(datetime(2026, 10, 11, 21, 59, tzinfo=UTC), 22) == datetime(2026, 10, 10, 22, tzinfo=UTC)
    assert last_boundary(datetime(2026, 10, 11, 22, 0, tzinfo=UTC), 22) == datetime(2026, 10, 11, 22, tzinfo=UTC)


# ---- AI review ---------------------------------------------------------------------

class FakeLLM:
    available = True

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def call(self, agent, system, user, model):
        self.calls.append(user)

        class R:
            output = PropReview(verdict=self.verdict, confidence=80, reason="test reason")
        return R()


VIEW = CoinView("ETH", 4000.0, 0.25, 1, True, 12.0, 4.0, 2.0)


@pytest.mark.parametrize("mode,verdict,allowed", [("shadow", "veto", True), ("veto", "veto", False),
                                                  ("veto", "approve", True)])
def test_ai_review_modes(db, clock, mode, verdict, allowed):
    r = PropReviewer(mode, FakeLLM(verdict), db, clock, {"ETH": ["ethereum", "eth"]})
    ok, rv = r.review(VIEW, 0.05, True)
    assert ok is allowed and rv.verdict == verdict
    row = db.query_one("SELECT * FROM prop_reviews")
    assert row["mode"] == mode and row["blocked"] == int(not allowed)


def test_ai_review_sees_matching_headlines_only(db, clock):
    for title in ("Ethereum upgrade goes live", "Solana outage resolved", "ETH ETF inflows jump"):
        db.execute("INSERT INTO news_items(id, source, title, published_at, fetched_at, coins, event_kinds) "
                   "VALUES (?, 'test', ?, ?, ?, '[]', '[]')", (title, title, iso(clock.now()), iso(clock.now())))
    llm = FakeLLM("approve")
    PropReviewer("shadow", llm, db, clock, {"ETH": ["ethereum", "eth"]}).review(VIEW, 0.05, True)
    assert "Ethereum upgrade" in llm.calls[0] and "ETF inflows" in llm.calls[0]
    assert "Solana" not in llm.calls[0]


def test_ai_review_off_makes_no_call(db, clock):
    llm = FakeLLM("veto")
    ok, rv = PropReviewer("off", llm, db, clock).review(VIEW, 0.05, True)
    assert ok and rv is None and llm.calls == []


def test_vetoed_buy_is_skipped_in_veto_mode(env):
    e = env
    e.eng.reviewer = PropReviewer("veto", FakeLLM("veto"), e.db, e.clock)
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}


# ---- live signals = research -------------------------------------------------------

def synthetic_frames(end: datetime, coins=("BTC", "ETH", "XRP", "SOL"), days=420, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end - timedelta(days=days), end - timedelta(hours=1), freq="1h", tz="UTC")
    out = {}
    for i, c in enumerate(coins):
        price = 100 * np.exp(np.cumsum(rng.normal(0.0003 * (i + 1), 0.01, len(idx))))
        out[c] = pd.DataFrame({"open": price, "high": price * 1.003, "low": price * 0.997, "close": price,
                               "volume": 1.0}, index=idx)
    return out


def test_live_rotation_matches_the_research_code():
    from desk.prop.market import build_market
    from desk.prop.strategies import rotation
    cfg = baseline_config()
    end = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    frames = synthetic_frames(end)
    ls = LiveSignals(cfg, loader=lambda s, e: {k: v[(v.index >= s) & (v.index < e)] for k, v in frames.items()})
    d = ls.rotation(end + timedelta(minutes=2))
    assert d is not None and d.candle_close == end
    m = build_market(frames, end - timedelta(days=30), end)
    st = cfg.prop.strategy
    sig = rotation(m, lookback=st.lookback_hours, top=st.top, stop_atr_d=st.stop_atr_days, shorts=False)
    t = m.T - 1
    assert set(d.buy) == {m.coins[j] for j in range(m.N) if sig.entry[t, j] == 1}
    assert d.sell == {m.coins[j] for j in range(m.N) if sig.exit_long[t, j]}


def test_live_rotation_waits_for_the_rebalance_candle():
    cfg = baseline_config()
    end = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)          # last candle closes at 05:00, not 00:00
    frames = synthetic_frames(end)
    ls = LiveSignals(cfg, loader=lambda s, e: {k: v[(v.index >= s) & (v.index < e)] for k, v in frames.items()})
    assert ls.rotation(end) is None


def test_stop_out_is_booked_to_the_right_account(db):
    clock = FakeClock(START, tz="Australia/Brisbane")
    db.clock = clock
    cfg = baseline_config()
    cfg = cfg.model_copy(update={"prop": cfg.prop.model_copy(update={"ai_review": "off", "paper_accounts": 2})})
    rules = tradeify_2step(100_000)
    prices = FakePrices(ETH=4_000.0, BTC=100_000.0)
    broker = PaperPropBroker(db, clock, rules, prices, ["paper-1", "paper-2"], cache_seconds=0)
    sig = FakeSignals()
    eng = PropEngine(cfg, rules, broker, sig, db, clock, MemoryNotifier(), ControlPanel(db, clock), sleep=lambda s: None)
    eng.startup()
    sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    eng.minute()
    assert set(eng.notes("paper-1")) == set(eng.notes("paper-2")) == {"ETH"}     # same trade on both accounts
    broker.move_stop("paper-1", "ETH", 1.0, None)                               # only paper-2's stop is hit
    db.execute("UPDATE prop_positions SET stop = 1.0 WHERE account = 'paper-1'")
    prices.m["ETH"] = 3_700.0
    clock.advance(minutes=1)
    eng.minute()
    assert "ETH" in eng.notes("paper-1") and eng.notes("paper-2") == {}
    assert db.query_one("SELECT account FROM prop_trades")[0] == "paper-2"



def test_missing_stop_order_is_retried(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    e.db.execute("UPDATE prop_positions SET stop_ref = NULL")
    e.clock.advance(minutes=1)
    e.eng.minute()
    assert e.eng.notes("paper-1")["ETH"].stop_ref is not None


def test_backup_close_if_the_platform_stop_did_not_fire(env):
    e = env
    e.sig.decision = decision(START.replace(minute=0), {"ETH": (4_000.0, 0.05)})
    e.eng.minute()
    n = e.eng.notes("paper-1")["ETH"]
    e.broker.move_stop("paper-1", "ETH", 1.0, None)          # the "platform" stop is somewhere useless
    e.prices.m["ETH"] = n.stop * 0.995                       # price is through the bot's own stop
    e.clock.advance(minutes=1)
    e.eng.minute()
    assert e.eng.notes("paper-1") == {}
    assert e.db.query_one("SELECT reason FROM prop_trades")[0] == "stop_backup"


def test_status_shows_cents(env):
    from desk.prop.brokers import AccountSnapshot
    from desk.prop.status import prop_status_text
    e = env
    text = prop_status_text(e.db, e.clock, e.cfg, {"paper-1": AccountSnapshot("paper-1", 99_999.92, 99_999.92)})
    assert "equity $99,999.92 (-0.08, -0.00%)" in text and "balance $99,999.92" in text


def test_scale_up_when_ahead_is_capped(env):
    e = env
    risk = e.cfg.prop.risk.model_copy(update={"scale_up_max": 1.5})
    e.eng.p = e.cfg.prop.model_copy(update={"risk": risk})
    assert e.eng.risk_for(100_000) == pytest.approx(1.0)
    assert e.eng.risk_for(103_000) == pytest.approx(1.5)          # 9% cushion of 6% -> capped at 1.5x
    assert e.eng.risk_for(97_000) == pytest.approx(0.5)
