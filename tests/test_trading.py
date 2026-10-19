"""End-to-end tests of the trading desk with a fake CoinSpot, a stub scanner
and a scripted AI. No network, no real money, no real AI."""

import re
from datetime import timedelta

import httpx
import pytest

from desk.broker.paper import PaperBroker
from desk.clock import iso
from desk.config import Secrets
from desk.data.market import MarketData
from desk.exchange.coinspot import WithdrawalProbe
from desk.llm import LLMClient
from desk.news import NewsFeed
from desk.notify import MemoryNotifier
from desk.scanner import CoinScan, Setup
from desk.trading import StartupRefused, TradingDesk
from tests.fakes import FakeAnthropic, FakeCoinSpot, fake_response


class StubScanner:
    """Returns healthy scans; tests decide which coins have setups or warning signs."""

    def __init__(self, clock):
        self.clock = clock
        self.setups: dict[str, list] = {}
        self.alerts: dict[str, list] = {}

    def flag(self, coin):
        self.setups[coin] = [Setup("trend_pullback", ["test setup"], 1.0, 2.5)]

    def scan(self, snapshot, coins=None, cycle_id=None):
        return [CoinScan(coin=c, at=self.clock.now(), quote_at=q.fetched_at, price_aud=q.mid, bid=q.bid, ask=q.ask,
                         spread_pct=q.spread_pct, trend="up", setups=list(self.setups.get(c, [])),
                         alerts=list(self.alerts.get(c, [])))
                for c, q in snapshot.quotes.items()]


class AI:
    """Scripted agents. Change the attributes to change their behaviour."""

    def __init__(self):
        self.entry = "BUY"
        self.target_pct = 2.5
        self.stop_pct = 1.0
        self.buy_vote = "BUY"
        self.sell_vote = "SELL"
        self.exit_action = "HOLD"
        self.new_stop = 0.0
        self.major = set()

    def __call__(self, kw):
        title = kw["output_config"]["format"]["schema"]["title"]
        msg = kw["messages"][0]["content"]
        if title == "NewsAssessment":
            coins = re.search(r"Coins to score: (.*)", msg).group(1).split(", ")
            return fake_response({"coins": [{"coin": c, "sentiment": 0.1, "reason": "routine",
                                             "major_event": c in self.major,
                                             "event_summary": "network halted" if c in self.major else ""}
                                            for c in coins]})
        if title == "EntryProposal":
            coin = re.search(r"Coin: (\w+)", msg).group(1)
            ask = float(re.search(r"ask A\$([\d,\.]+)", msg).group(1).replace(",", ""))
            if self.entry == "NO_TRADE":
                return fake_response({"coin": coin, "decision": "NO_TRADE", "entry_aud": 0, "stop_loss_aud": 0,
                                      "take_profit_aud": 0, "confidence": 30, "reasoning": "pass"})
            return fake_response({"coin": coin, "decision": "BUY", "entry_aud": ask,
                                  "stop_loss_aud": ask * (1 - self.stop_pct / 100),
                                  "take_profit_aud": ask * (1 + self.target_pct / 100),
                                  "confidence": 65, "reasoning": "scripted"})
        if title == "SkepticVerdict":
            vote = self.buy_vote if "REVIEW: BUY" in msg else self.sell_vote
            return fake_response({"arguments_against": ["scripted"], "vote": vote, "reasoning": "scripted"})
        if title == "ExitReview":
            ids = re.findall(r"Position id: (\S+)", msg)
            return fake_response({"positions": [{"position_id": i, "action": self.exit_action,
                                                 "new_stop_aud": self.new_stop, "confidence": 60,
                                                 "reasoning": "scripted"} for i in ids]})
        raise AssertionError(title)


@pytest.fixture
def make(config, db, clock):
    def _make(*, ai: AI | None = None, secrets: Secrets | None = None, **risk_overrides):
        cfg = config.model_copy(update={
            "news": config.news.model_copy(update={"rss_feeds": []}),           # no network in tests
            "risk": config.risk.model_copy(update=risk_overrides),
        })
        cs = FakeCoinSpot(clock, {"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0}, spread_pct=0.1)
        fake = FakeAnthropic(*([ai] * 200)) if ai else None
        llm = LLMClient(cfg.llm, db, clock, client=fake)
        desk = TradingDesk(config=cfg, secrets=secrets or Secrets(), clock=clock, db=db,
                           market=MarketData(cs, cfg.coins, clock), scanner=StubScanner(clock),
                           news=NewsFeed(cfg, httpx.Client(), clock, db), llm=llm, broker=PaperBroker(cs, cfg),
                           api_health=cs.health, notifier=MemoryNotifier(), coinspot=cs)
        return desk, cs, fake
    return _make


def add_headline(desk, title, coins, kinds=()):
    from desk.news import NewsItem
    desk.news._store(NewsItem(id=title, source="test", title=title, url=None, summary="",
                              published_at=desk.clock.now() - timedelta(minutes=5), coins=coins,
                              event_kinds=list(kinds)))


def test_news_is_scored_when_there_are_headlines(make):
    desk, cs, fake = make(ai=AI())
    add_headline(desk, "Bitcoin ETF sees inflows", ["BTC"])
    desk.scanner.flag("BTC")
    desk.agent_cycle()
    titles = [c["output_config"]["format"]["schema"]["title"] for c in fake.calls]
    assert titles == ["NewsAssessment", "EntryProposal", "SkepticVerdict"]


def kinds(desk):
    return [k for k, _ in desk.notify.sent]


def open_btc(desk):
    desk.scanner.flag("BTC")
    rep = desk.agent_cycle()
    desk.scanner.setups.clear()
    assert rep.opened, rep.line()
    return desk.portfolio.open_positions()[0]


# ---- the full chain ------------------------------------------------------------

def test_flagged_coin_is_debated_risk_checked_and_bought(make, db):
    desk, cs, fake = make(ai=AI())
    desk.startup()
    desk.scanner.flag("BTC")
    rep = desk.agent_cycle()
    assert rep.flagged == ["BTC"] and len(rep.opened) == 1
    assert len(fake.calls) == 2      # entry + skeptic (no headlines -> the News Analyst needs no AI call)
    pos = desk.portfolio.open_positions()[0]
    assert pos.coin == "BTC" and pos.stop_loss < pos.entry_price < pos.take_profit
    cost = pos.qty * pos.entry_price + pos.entry_fee_aud
    assert cost <= 10_000 * 0.05 + 1e-6                          # 5% max per trade
    assert desk.portfolio.cash_aud() == pytest.approx(10_000 - cost)
    assert db.query_one("SELECT outcome FROM debates")[0] == "approved"
    assert db.query_one("SELECT approved FROM risk_decisions")[0] == 1
    assert "trade" in kinds(desk)
    assert db.kv_get("heartbeat_cycle")


def test_nothing_flagged_means_no_ai_calls(make):
    desk, cs, fake = make(ai=AI())
    rep = desk.agent_cycle()
    assert rep.flagged == [] and fake.calls == []


def test_no_trade_and_skeptic_rejection_buy_nothing(make):
    ai = AI()
    ai.entry = "NO_TRADE"
    desk, cs, fake = make(ai=ai)
    desk.scanner.flag("BTC")
    assert desk.agent_cycle().debates == ["BTC: no_trade"]
    ai.entry, ai.buy_vote = "BUY", "SKIP"
    assert desk.agent_cycle().debates == ["BTC: rejected"]
    assert desk.portfolio.open_positions() == []


def test_risk_manager_veto_blocks_agreed_trade(make):
    ai = AI()
    ai.target_pct = 0.5                                           # too small to beat costs
    desk, cs, fake = make(ai=ai)
    desk.scanner.flag("BTC")
    rep = desk.agent_cycle()
    assert desk.portfolio.open_positions() == []
    assert "Risk Manager said no" in rep.debates[0]
    assert "risk_veto" in kinds(desk)


# ---- code exits on the fast tick ---------------------------------------------------

def test_stop_loss_sells_on_fast_tick(make, db):
    desk, cs, _ = make(ai=AI())
    pos = open_btc(desk)
    desk.fast_tick()
    assert len(desk.portfolio.open_positions()) == 1               # price unchanged: hold
    cs.mid["BTC"] = 98_000.0
    desk.fast_tick()
    assert desk.portfolio.open_positions() == []
    sell = db.query_one("SELECT * FROM trades WHERE side='sell'")
    assert sell["reason"] == "stop_loss" and sell["gain_aud"] < 0
    assert db.query_one("SELECT approved FROM risk_decisions WHERE kind='exit'")[0] == 1


def test_trailing_stop_locks_in_profit(make, db):
    desk, cs, _ = make(ai=AI())
    open_btc(desk)
    cs.mid["BTC"] = 101_500.0
    desk.fast_tick()
    p = desk.portfolio.open_positions()[0]
    assert p.trailing_active and p.highest_price > 101_000
    assert any("trailing stop is now active" in t for _, t in desk.notify.sent)
    cs.mid["BTC"] = 100_500.0                                       # pulls back below the trailing stop
    desk.fast_tick()
    sell = db.query_one("SELECT * FROM trades WHERE side='sell'")
    assert sell["reason"] == "trailing_stop" and sell["gain_aud"] > 0


def test_take_profit(make, db):
    desk, cs, _ = make(ai=AI())
    open_btc(desk)
    cs.mid["BTC"] = 103_000.0
    desk.fast_tick()
    assert db.query_one("SELECT reason FROM trades WHERE side='sell'")[0] == "take_profit"


def test_max_hold_time(make, db, clock):
    desk, cs, _ = make(ai=AI())
    open_btc(desk)
    clock.advance(hours=24, seconds=1)
    desk.fast_tick()
    assert db.query_one("SELECT reason FROM trades WHERE side='sell'")[0] == "max_hold"


def test_equity_snapshot_every_fast_tick(make, db):
    desk, cs, _ = make(ai=AI())
    desk.fast_tick()
    desk.fast_tick()
    assert db.query_one("SELECT COUNT(*) FROM equity_snapshots")[0] == 2
    assert db.kv_get("heartbeat_fast")


def test_fast_tick_runs_the_daily_clean_up_and_survives_it_failing(make, db, clock, monkeypatch):
    desk, cs, _ = make(ai=AI())
    desk.fast_tick()
    assert db.kv_get(f"housekeeping_done:{clock.local_date().isoformat()}")

    def broken(db_, clock_):
        raise RuntimeError("disk full")

    monkeypatch.setattr("desk.trading.run_housekeeping", broken)
    desk.fast_tick()                                   # clean-up failing must not stop the price checks
    assert db.kv_get("heartbeat_fast") == iso(clock.now())


# ---- switches --------------------------------------------------------------------

def test_pause_blocks_entries_without_spending_on_ai(make):
    desk, cs, fake = make(ai=AI())
    desk.controls.pause(by="test")
    desk.scanner.flag("BTC")
    rep = desk.agent_cycle()
    assert rep.skipped_entries == "paused" and fake.calls == [] and not rep.opened


def test_kill_stops_ai_but_stops_still_protect(make, db):
    desk, cs, fake = make(ai=AI())
    open_btc(desk)
    calls_before = len(fake.calls)
    desk.controls.kill(by="test")
    desk.scanner.flag("ETH")
    desk.scanner.alerts["BTC"] = ["15m MACD crossed below its signal line"]
    rep = desk.agent_cycle()
    assert rep.skipped_entries == "kill switch on" and len(fake.calls) == calls_before
    cs.mid["BTC"] = 98_000.0
    desk.fast_tick()
    assert desk.portfolio.open_positions() == []                   # stop-loss still fired while killed


def test_kill_close_all_sells_everything(make, db):
    desk, cs, _ = make(ai=AI())
    open_btc(desk)
    desk.scanner.flag("ETH")
    desk.agent_cycle()
    assert len(desk.portfolio.open_positions()) == 2
    desk.controls.kill(by="test", close_all=True)
    desk.fast_tick()
    assert desk.portfolio.open_positions() == []
    assert {r[0] for r in db.query("SELECT reason FROM trades WHERE side='sell'")} == {"kill_close"}
    assert not desk.controls.state().close_all_requested
    assert any("Close-all done" in t for _, t in desk.notify.sent)


def test_daily_loss_limit_pauses_new_trades_until_midnight(make, db, clock):
    desk, cs, fake = make(ai=AI())
    desk.fast_tick()                                                # records today's starting equity
    db.kv_set(f"day_start_equity:paper:{clock.local_date().isoformat()}", "10400")   # pretend we're down ~3.8%
    desk.fast_tick()
    assert desk.controls.state().daily_paused
    assert "risk_limit" in kinds(desk)
    desk.scanner.flag("ETH")
    rep = desk.agent_cycle()
    assert rep.skipped_entries == "daily loss limit hit" and fake.calls == []
    desk.fast_tick()
    assert kinds(desk).count("risk_limit") == 1                     # alerted once, not every minute
    clock.set(clock.next_day_start_utc() + timedelta(minutes=1))   # next day: trading allowed again
    assert desk.agent_cycle().opened


def test_max_open_positions_gate_saves_ai_money(make):
    desk, cs, fake = make(ai=AI(), max_open_positions=1)
    open_btc(desk)
    n = len(fake.calls)
    desk.scanner.flag("ETH")
    rep = desk.agent_cycle()
    assert "positions open" in rep.skipped_entries and len(fake.calls) == n


def test_ai_budget_used_up(make, db, clock):
    desk, cs, fake = make(ai=AI())
    db.execute("INSERT INTO llm_calls(ts, agent, model, input_tokens, output_tokens, cache_write_tokens, "
               "cache_read_tokens, cost_usd, latency_ms, ok) VALUES (?, 'x', 'm', 0, 0, 0, 0, 5.0, 0, 1)",
               (clock.now().isoformat(),))
    desk.scanner.flag("BTC")
    assert desk.agent_cycle().skipped_entries == "daily AI budget used up"
    desk.agent_cycle()
    assert fake.calls == [] and kinds(desk).count("risk_limit") == 1


# ---- AI exit reviews -------------------------------------------------------------

def test_exit_agent_and_skeptic_can_sell(make, db):
    ai = AI()
    desk, cs, fake = make(ai=ai)
    open_btc(desk)
    ai.exit_action = "SELL"
    desk.scanner.alerts["BTC"] = ["1h price below EMA50 (trend weakening)"]
    rep = desk.agent_cycle()
    assert rep.closed and desk.portfolio.open_positions() == []
    assert db.query_one("SELECT reason FROM trades WHERE side='sell'")[0] == "agent_sell"


def test_exit_agent_sell_needs_skeptic(make):
    ai = AI()
    desk, cs, fake = make(ai=ai)
    open_btc(desk)
    ai.exit_action, ai.sell_vote = "SELL", "WAIT"
    desk.scanner.alerts["BTC"] = ["warning"]
    desk.agent_cycle()
    assert len(desk.portfolio.open_positions()) == 1


def test_exit_agent_tightens_stop(make):
    ai = AI()
    desk, cs, fake = make(ai=ai)
    pos = open_btc(desk)
    ai.exit_action, ai.new_stop = "TIGHTEN_STOP", pos.stop_loss + 300
    desk.scanner.alerts["BTC"] = ["warning"]
    desk.agent_cycle()
    assert desk.portfolio.open_positions()[0].stop_loss == pytest.approx(pos.stop_loss + 300)


def test_quiet_held_position_costs_no_ai(make):
    desk, cs, fake = make(ai=AI())
    open_btc(desk)
    n = len(fake.calls)
    desk.agent_cycle()
    assert len(fake.calls) == n


def test_major_event_blocks_entry(make):
    ai = AI()
    ai.major = {"ETH"}
    desk, cs, fake = make(ai=ai)
    add_headline(desk, "Ethereum network halted after consensus bug", ["ETH"], ["outage"])
    desk.scanner.flag("ETH")
    rep = desk.agent_cycle()
    assert rep.debates == ["ETH: blocked by a major news event"] and not rep.opened
    assert len(fake.calls) == 1                                      # only the news call
    desk.agent_cycle()                                               # next cycle: block stays, no new calls
    assert len(fake.calls) == 1


# ---- start-up, recovery, outages -------------------------------------------------

def test_restart_recovers_positions_and_keeps_managing_them(make, db):
    desk1, cs1, _ = make(ai=AI())
    open_btc(desk1)
    desk2, cs2, _ = make(ai=AI())                                    # a fresh process on the same database
    desk2.startup()
    startup = [t for k, t in desk2.notify.sent if k == "startup"][0]
    assert "1 open position(s) recovered" in startup and "BTC" in startup
    cs2.mid["BTC"] = 98_000.0
    desk2.fast_tick()
    assert desk2.portfolio.open_positions() == []
    assert db.kv_get("restart_count") == "1"


def test_withdrawal_enabled_key_refuses_to_start(make):
    desk, cs, _ = make(ai=AI(), secrets=Secrets(coinspot_key="k", coinspot_secret="s"))
    cs.probe = WithdrawalProbe(True, "this key CAN withdraw funds")
    with pytest.raises(StartupRefused, match="WITHDRAW"):
        desk.startup()


def test_unknown_withdrawal_status_is_a_warning(make):
    desk, cs, _ = make(ai=AI(), secrets=Secrets(coinspot_key="k", coinspot_secret="s"))
    cs.probe = WithdrawalProbe(None, "couldn't reach CoinSpot")
    assert any("withdrawals off" in w for w in desk.startup())


def test_no_api_key_manages_positions_but_never_buys(make):
    desk, cs, _ = make(ai=None)
    assert any("ANTHROPIC_API_KEY" in w for w in desk.startup())
    desk.scanner.flag("BTC")
    rep = desk.agent_cycle()
    assert rep.skipped_entries == "no ANTHROPIC_API_KEY" and not rep.opened


def test_coinspot_outage_is_survived(make, db):
    desk, cs, _ = make(ai=AI())
    cs.down = True
    for _ in range(3):
        desk.fast_tick()                                             # must not raise
    assert db.kv_get("heartbeat_fast")
    assert "error" in kinds(desk)                                    # alerted after 3 failures in a row
    rep = desk.agent_cycle()
    assert "cycle skipped" in rep.notes[0]
    cs.down = False
    desk.fast_tick()
    assert db.query_one("SELECT COUNT(*) FROM equity_snapshots")[0] == 1


def test_daily_summary_sent_once_after_its_time(make, clock):
    desk, cs, _ = make(ai=AI())
    desk.fast_tick()
    assert "daily_summary" not in kinds(desk)                        # 19:00 local, before 23:55
    clock.set(clock.day_start_utc() + timedelta(hours=23, minutes=56))
    desk.fast_tick()
    desk.fast_tick()
    summaries = [t for k, t in desk.notify.sent if k == "daily_summary"]
    assert len(summaries) == 1 and "Equity A$" in summaries[0] and "AI spend today" in summaries[0]
