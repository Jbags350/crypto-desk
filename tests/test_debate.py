import json
from dataclasses import replace
from datetime import timedelta

import pytest

from desk.agents.context import market_context, position_context
from desk.agents.entry import sanity_problems
from desk.agents.exit_agent import HeldPosition, needs_review
from desk.agents.news_analyst import CoinNews
from desk.debate import Debate
from desk.llm import LLMClient
from desk.models import Position
from desk.scanner import CoinScan, Setup, TfView
from desk.schemas import EntryProposal
from tests.fakes import FakeAnthropic, fake_response

VIEW = TfView(timeframe="15m", candle_close_time="", close=100.0, prev_close=99.0, ema_fast=99.5, ema_mid=99.0,
              ema_slow=98.0, ema_trend=95.0, rsi=55.0, prev_rsi=50.0, min_rsi_recent=38.0, macd=0.1,
              macd_signal=0.05, hist=0.05, prev_hist=0.02, atr=0.5, atr_pct=0.5, vol_ratio=1.4, breakout_level=101.0)


def make_scan(clock, coin="BTC", alerts=(), ask=100_000.0):
    return CoinScan(coin=coin, at=clock.now(), price_aud=ask - 50, bid=ask - 100, ask=ask, spread_pct=0.1,
                    global_divergence_pct=0.05, trend="up",
                    views={"1h": replace(VIEW, timeframe="1h"), "15m": VIEW, "5m": replace(VIEW, timeframe="5m")},
                    setups=[Setup("trend_pullback", ["1h uptrend", "RSI turning up"], 0.75, 1.5)],
                    alerts=list(alerts))


NEWS = CoinNews("BTC", 0.2, "ETF inflows", False, "", 5, "ai")


def buy(coin="BTC", entry=100_000.0, stop=99_000.0, tp=102_500.0, conf=62):
    return {"coin": coin, "decision": "BUY", "entry_aud": entry, "stop_loss_aud": stop,
            "take_profit_aud": tp, "confidence": conf, "reasoning": "Pullback in uptrend with rising momentum."}


def verdict(vote):
    return {"arguments_against": ["Volume is only average", "Resistance overhead"], "vote": vote,
            "reasoning": "Holds up reasonably."}


@pytest.fixture
def desk(config, db, clock):
    def _make(*responses):
        fake = FakeAnthropic(*responses)
        return Debate(config, LLMClient(config.llm, db, clock, client=fake), db, clock), fake
    return _make


# ---- entry debates ----------------------------------------------------------

@pytest.mark.parametrize("vote,outcome", [("BUY", "approved"), ("WAIT", "wait"), ("SKIP", "rejected")])
def test_entry_debate_outcomes(desk, clock, db, vote, outcome):
    d, fake = desk(fake_response(buy()), fake_response(verdict(vote)))
    r = d.run_entry(make_scan(clock), NEWS, [], cycle_id="c1")
    assert r.outcome == outcome and r.approved == (vote == "BUY")
    assert [t["agent"] for t in r.transcript] == ["entry_agent", "skeptic"]
    assert len(fake.calls) == 2
    row = db.query_one("SELECT * FROM debates WHERE id = ?", (r.id,))
    assert row["outcome"] == outcome and row["cycle_id"] == "c1" and row["kind"] == "entry"
    assert len(json.loads(row["transcript"])) == 2
    assert db.query_one("SELECT COUNT(*) FROM agent_outputs")[0] == 2


def test_skeptic_voting_sell_on_a_buy_counts_as_no(desk, clock):
    d, _ = desk(fake_response(buy()), fake_response(verdict("SELL")))
    r = d.run_entry(make_scan(clock), NEWS, [])
    assert r.outcome == "rejected" and "SELL on a buy" in r.problems[0]


def test_no_trade_skips_skeptic(desk, clock):
    no = {**buy(), "decision": "NO_TRADE", "entry_aud": 0, "stop_loss_aud": 0, "take_profit_aud": 0}
    d, fake = desk(fake_response(no))
    r = d.run_entry(make_scan(clock), NEWS, [])
    assert r.outcome == "no_trade" and len(fake.calls) == 1


@pytest.mark.parametrize("proposal,msg", [
    (buy(stop=100_500.0), "stop-loss < entry"),                  # stop above entry
    (buy(tp=99_500.0), "stop-loss < entry"),                     # take-profit below entry
    (buy(entry=101_000.0, stop=100_000.0, tp=103_000.0), "from the CoinSpot ask"),   # 1% away from ask
    (buy(coin="ETH"), "setup was for BTC"),
    (buy(stop=0.0), "stop-loss < entry"),                        # missing stop
])
def test_invalid_buy_never_reaches_skeptic(desk, clock, proposal, msg):
    d, fake = desk(fake_response(proposal))
    r = d.run_entry(make_scan(clock), NEWS, [])
    assert r.outcome == "invalid" and any(msg in p for p in r.problems)
    assert len(fake.calls) == 1


def test_entry_agent_failure_is_error_not_approval(desk, clock):
    d, fake = desk(fake_response({"garbage": True}))
    r = d.run_entry(make_scan(clock), NEWS, [])
    assert r.outcome == "error" and not r.approved and "Entry Agent failed" in r.problems[0]


def test_skeptic_failure_is_error_not_approval(desk, clock):
    d, fake = desk(fake_response(buy()), fake_response("not json"))
    r = d.run_entry(make_scan(clock), NEWS, [])
    assert r.outcome == "error" and not r.approved and "Skeptic failed" in r.problems[0]


def test_no_api_key_is_error(config, db, clock):
    d = Debate(config, LLMClient(config.llm, db, clock, api_key=None), db, clock)
    r = d.run_entry(make_scan(clock), NEWS, [])
    assert r.outcome == "error" and "ANTHROPIC_API_KEY" in r.problems[0]


def test_entry_message_contents(desk, clock):
    d, fake = desk(fake_response(buy()), fake_response(verdict("BUY")))
    held = [Position("ETH", 1, 3000, 2900, 3100, clock.now(), 3000)]
    d.run_entry(make_scan(clock), NEWS, held)
    entry_msg = fake.calls[0]["messages"][0]["content"]
    for expected in ("Coin: BTC", "ask A$100,000.00", "Round-trip trading cost", "trend_pullback",
                     "News mood: +0.20", "Open positions: 1 of max 3 (ETH)", "Reward:risk",
                     "at most 5.0% below"):
        assert expected in entry_msg, expected
    skeptic_msg = fake.calls[1]["messages"][0]["content"]
    assert "PROPOSAL TO REVIEW: BUY" in skeptic_msg and "Reward:risk 2.50" in skeptic_msg
    assert "Entry Agent confidence: 62/100" in skeptic_msg
    assert "Skeptic" in fake.calls[1]["system"][0]["text"]


def test_sanity_ok_for_good_buy(config, clock):
    assert sanity_problems(EntryProposal.model_validate(buy()), make_scan(clock), config) == []


# ---- exit reviews -------------------------------------------------------------

def held_position(clock, coin="BTC", alerts=("15m MACD crossed below its signal line",), news=None, pid=7):
    p = Position(coin, 0.01, 100_000.0, 99_000.0, 102_500.0, clock.now() - timedelta(hours=3), 100_800.0, id=pid)
    return HeldPosition(p, bid=100_600.0, scan=make_scan(clock, coin, alerts), news=news)


def advice(pid="7", action="HOLD", new_stop=0.0):
    return {"positions": [{"position_id": pid, "action": action, "new_stop_aud": new_stop, "confidence": 60,
                           "reasoning": "test"}]}


def test_needs_review_rules(clock):
    assert needs_review(held_position(clock))
    assert not needs_review(held_position(clock, alerts=()))
    bad_news = CoinNews("BTC", -0.6, "hack rumours", False, "", 3, "ai")
    assert needs_review(held_position(clock, alerts=(), news=bad_news))
    major = CoinNews("BTC", -0.1, "x", True, "exchange hacked", 3, "ai")
    assert needs_review(held_position(clock, alerts=(), news=major))


def test_quiet_positions_cost_nothing(desk, clock):
    d, fake = desk()
    assert d.run_exits([held_position(clock, alerts=())], clock.now()) == []
    assert fake.calls == []


def test_exit_hold(desk, clock):
    d, fake = desk(fake_response(advice(action="HOLD")))
    [r] = d.run_exits([held_position(clock)], clock.now())
    assert r.outcome == "hold" and len(fake.calls) == 1


@pytest.mark.parametrize("vote,outcome", [("SELL", "approved"), ("WAIT", "wait"), ("SKIP", "rejected"),
                                          ("BUY", "rejected")])
def test_exit_sell_goes_to_skeptic(desk, clock, vote, outcome):
    d, fake = desk(fake_response(advice(action="SELL")), fake_response(verdict(vote)))
    [r] = d.run_exits([held_position(clock)], clock.now())
    assert r.outcome == outcome and len(fake.calls) == 2
    assert "PROPOSAL TO REVIEW: SELL" in fake.calls[1]["messages"][0]["content"]


def test_tighten_valid_skips_skeptic(desk, clock):
    d, fake = desk(fake_response(advice(action="TIGHTEN_STOP", new_stop=100_000.0)))
    [r] = d.run_exits([held_position(clock)], clock.now())
    assert r.outcome == "tighten" and r.new_stop == 100_000.0 and len(fake.calls) == 1


@pytest.mark.parametrize("new_stop", [98_000.0, 100_590.0, 101_000.0])
def test_tighten_invalid_keeps_old_stop(desk, clock, new_stop):
    d, _ = desk(fake_response(advice(action="TIGHTEN_STOP", new_stop=new_stop)))
    [r] = d.run_exits([held_position(clock)], clock.now())
    assert r.outcome == "hold" and r.new_stop is None and "stop not changed" in r.problems[0]


def test_exit_agent_failure_keeps_positions(desk, clock):
    d, _ = desk(fake_response("nonsense"))
    rs = d.run_exits([held_position(clock), held_position(clock, "ETH", pid=8)], clock.now())
    assert [r.outcome for r in rs] == ["error", "error"]


def test_missing_advice_means_hold(desk, clock):
    d, _ = desk(fake_response(advice(pid="999")))
    [r] = d.run_exits([held_position(clock)], clock.now())
    assert r.outcome == "hold" and "no advice" in r.problems[0]


def test_force_reviews_quiet_positions(desk, clock):
    d, fake = desk(fake_response(advice()))
    assert len(d.run_exits([held_position(clock, alerts=())], clock.now(), force=True)) == 1


def test_position_context_text(config, clock):
    h = held_position(clock)
    txt = position_context(h.position, h.bid, clock.now(), config)
    assert "Position id: 7" in txt and "bought at A$100,000.00 3.0 hours ago" in txt
    assert "+0.60% vs entry" in txt and "not active yet" in txt


def test_market_context_marks_blocked_news(config, clock):
    blocked = CoinNews("BTC", -0.5, "x", True, "exchange hacked", 2, "ai", blocked=True)
    txt = market_context(make_scan(clock), blocked, config)
    assert "MAJOR EVENT: exchange hacked" in txt and "BLOCKED" in txt
