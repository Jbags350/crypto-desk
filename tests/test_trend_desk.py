"""Trend mode in the live/paper desk: daily signal -> debate -> Risk Manager -> trailing-stop exits."""

import re
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from desk.broker.paper import PaperBroker
from desk.config import Secrets
from desk.data.candles import CandleSet
from desk.data.market import MarketData
from desk.llm import LLMClient
from desk.main import cycle_schedule, next_cycle_time
from desk.news import NewsFeed
from desk.notify import MemoryNotifier
from desk.strategy import apply_strategy
from desk.trading import TradingDesk
from tests.fakes import FakeAnthropic, FakeCoinSpot, fake_response


class TrendFeed:
    """Daily candles: flat for months, then (for `breakout` coins) a breakout on the last day."""

    def __init__(self, clock, coinspot, breakout=("BTC",)):
        self.clock, self.cs, self.breakout = clock, coinspot, set(breakout)

    def get(self, coin, tf, limit):
        assert tf == "1d"
        now = self.clock.now()
        last_open = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=1)
        idx = pd.date_range(end=last_open, periods=150, freq="1D", tz="UTC")
        close = np.array([100.0 + (i % 3) for i in range(150)])
        if coin.code in self.breakout:
            close[-1] = 115.0
        df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                           "volume": 1.0}, index=pd.DatetimeIndex(idx, name="open_time"))
        return CandleSet(coin.code, "1d", df, "binance", "AUD", now)

    def spot_price(self, coin):
        return self.cs.mid[coin.code], "AUD", "binance"


class AI:
    def __init__(self):
        self.decision = "BUY"

    def __call__(self, kw):
        title = kw["output_config"]["format"]["schema"]["title"]
        msg = kw["messages"][0]["content"]
        if title == "EntryProposal":
            coin = re.search(r"Coin: (\w+)", msg).group(1)
            # deliberately silly levels: trend mode must use the RULE's levels, not these
            return fake_response({"coin": coin, "decision": self.decision, "entry_aud": 1, "stop_loss_aud": 2,
                                  "take_profit_aud": 3, "confidence": 70, "reasoning": "clean breakout"})
        if title == "SkepticVerdict":
            return fake_response({"arguments_against": ["could fade"], "vote": "BUY", "reasoning": "ok"})
        raise AssertionError(title)


@pytest.fixture
def make(config, db, clock):
    def _make(*, ai=None, breakout=("BTC",)):
        cfg = apply_strategy(config.model_copy(update={
            "strategy": config.strategy.model_copy(update={"mode": "trend"}),
            "risk": config.risk.model_copy(update={"max_position_pct": 10.0}),
            "news": config.news.model_copy(update={"rss_feeds": []}),
        }))
        cs = FakeCoinSpot(clock, {"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0}, spread_pct=0.1)
        fake = FakeAnthropic(*([ai] * 50)) if ai else None
        desk = TradingDesk(config=cfg, secrets=Secrets(), clock=clock, db=db,
                           market=MarketData(cs, cfg.coins, clock), scanner=object(),
                           news=NewsFeed(cfg, httpx.Client(), clock, db), llm=LLMClient(cfg.llm, db, clock, client=fake),
                           broker=PaperBroker(cs, cfg), api_health=cs.health, notifier=MemoryNotifier(),
                           coinspot=cs, candles=TrendFeed(clock, cs, breakout))
        return desk, cs, fake
    return _make


def test_breakout_is_debated_and_bought_with_the_rules_levels(make, db):
    desk, cs, fake = make(ai=AI())
    rep = desk.agent_cycle()
    assert rep.flagged == ["BTC"] and len(rep.opened) == 1, rep.line()
    assert len(fake.calls) == 2                                       # entry + skeptic (no headlines -> no news call)
    assert "trend-following" in fake.calls[0]["system"][0]["text"]
    assert "The trend rule has signalled a breakout" in fake.calls[0]["messages"][0]["content"]
    p = desk.portfolio.open_positions()[0]
    row = db.query_one("SELECT * FROM positions")
    assert row["strategy"] == "trend" and row["trailing_active"] == 1
    assert row["max_hold_hours"] == pytest.approx(120 * 24)
    trail = row["trail_pct"]
    assert 2 < trail < 15                                             # 2 daily ATRs, from the candles
    assert p.stop_loss == pytest.approx(cs.latest_all()["BTC"].ask * (1 - trail / 100), rel=1e-3)
    assert p.take_profit > p.entry_price * 5                          # no fixed target
    cost = p.qty * p.entry_price + p.entry_fee_aud
    assert 900 < cost <= 1_000 + 1e-6                                 # 10% per trade


def test_same_daily_signal_is_only_debated_once(make):
    desk, cs, fake = make(ai=AI())
    desk.agent_cycle()
    n = len(fake.calls)
    desk.controls.request_close_all(by="test")
    desk.fast_tick()                                                  # sell it, so "already holding" isn't the reason
    desk.agent_cycle()
    assert len(fake.calls) == n


def test_no_breakout_no_ai(make):
    desk, cs, fake = make(ai=AI(), breakout=())
    rep = desk.agent_cycle()
    assert rep.flagged == [] and fake.calls == []


def test_ai_veto_is_respected_and_remembered(make):
    ai = AI()
    ai.decision = "NO_TRADE"
    desk, cs, fake = make(ai=ai)
    rep = desk.agent_cycle()
    assert rep.opened == [] and rep.debates == ["BTC: no_trade"]
    desk.agent_cycle()
    assert len(fake.calls) == 1


def test_signal_missed_when_bot_was_off_at_the_close(make, clock):
    desk, cs, fake = make(ai=AI())
    clock.set(datetime(2026, 10, 9, 13, 0, tzinfo=UTC))              # 13h after the daily close
    rep = desk.agent_cycle()
    assert rep.flagged == ["BTC"] and fake.calls == []
    assert "signal missed" in rep.debates[0]


def test_trailing_stop_rides_the_trend_then_sells_in_profit(make, db):
    desk, cs, fake = make(ai=AI())
    desk.agent_cycle()
    p = desk.portfolio.open_positions()[0]
    cs.mid["BTC"] = 130_000.0                                         # trend runs +30%
    desk.fast_tick()
    p2 = desk.portfolio.open_positions()[0]
    assert p2.highest_price > 129_000
    stop_now = p2.highest_price * (1 - p2.trail_pct / 100)
    assert stop_now > p.entry_price                                   # the trailing stop has locked in a gain
    cs.mid["BTC"] = stop_now * 0.99                                   # trend breaks
    desk.fast_tick()
    sell = db.query_one("SELECT * FROM trades WHERE side='sell'")
    assert sell["reason"] == "trailing_stop" and sell["gain_aud"] > 0


def test_paused_means_no_ai_and_no_trade(make):
    desk, cs, fake = make(ai=AI())
    desk.controls.pause(by="test")
    rep = desk.agent_cycle()
    assert rep.skipped_entries == "paused" and fake.calls == []


def test_daily_schedule():
    now = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
    assert next_cycle_time(now, 1440, 60) == datetime(2026, 10, 10, 0, 1, tzinfo=UTC)   # 10:01 Brisbane
    assert next_cycle_time(datetime(2026, 10, 9, 0, 0, 30, tzinfo=UTC), 1440, 60) == \
        datetime(2026, 10, 9, 0, 1, tzinfo=UTC)


def test_cycle_schedule_by_mode(config):
    assert cycle_schedule(config) == (config.schedule.agent_cycle_minutes, config.schedule.cycle_delay_seconds)
    trend = config.model_copy(update={"strategy": config.strategy.model_copy(update={"mode": "trend"})})
    assert cycle_schedule(trend) == (1440, 60)
