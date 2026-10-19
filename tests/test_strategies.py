"""Strategy modes (day / swing / trend) and limit-order execution."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import desk.backtest.engine as engine
from desk.backtest.engine import Backtester, bar_exit
from desk.backtest.history import resample
from desk.exits import check_exit
from desk.models import Position
from desk.risk import Controls, EntryRequest, MarketState, PortfolioState, RiskManager
from desk.scanner import Setup
from desk.strategy import apply_strategy, paper_bot_supports
from desk.trend import NO_TARGET_PCT, trend_frame, trend_signal

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def with_(config, **sections):
    upd = {}
    for name, changes in sections.items():
        upd[name] = getattr(config, name).model_copy(update=changes)
    return config.model_copy(update=upd)


def candles(start=T0, n=600, price=lambda i: 100.0, freq="5min", wick=0.002):
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC").as_unit("ns")
    close = np.array([price(i) for i in range(n)], dtype=float)
    opn = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": opn, "high": np.maximum(opn, close) * (1 + wick),
                         "low": np.minimum(opn, close) * (1 - wick), "close": close,
                         "volume": np.full(n, 10.0)}, index=pd.DatetimeIndex(idx, name="open_time"))


# ---- costs depend on the order type -----------------------------------------------------

@pytest.mark.parametrize("entry,tp,expected", [
    ("market", "market", 0.2 + 0.2 + 0.1),          # fees + full spread + 2 slippages (unchanged from before)
    ("limit", "market", 0.2 + 0.1 + 0.05),          # one market leg
    ("limit", "limit", 0.2),                          # fees only
])
def test_round_trip_cost_by_order_type(config, entry, tp, expected):
    cfg = with_(config, execution={"entry_order": entry, "take_profit_order": tp})
    assert RiskManager(cfg).round_trip_cost_pct(0.2) == pytest.approx(expected)


def test_risk_manager_prices_a_limit_buy_at_the_bid(config):
    cfg = with_(config, execution={"entry_order": "limit", "take_profit_order": "limit"})
    rm = RiskManager(cfg)
    req = EntryRequest("BTC", 99_900.0, 98_900.0, 101_900.0, 65, "BUY")
    pf = PortfolioState(10_000.0, 10_000.0, 10_000.0)
    d = rm.check_entry(req, pf, MarketState("BTC", 99_900.0, 100_100.0, 1.0), Controls(), entry_price=99_900.0)
    assert d.approved, d.reasons
    assert d.price == 99_900.0 and d.max_price == 99_900.0          # a limit order never pays more
    assert d.qty * d.max_price * 1.001 <= 500.0 + 1e-9
    # a "limit" price above the ask isn't a limit buy at all
    bad = rm.check_entry(req, pf, MarketState("BTC", 99_900.0, 100_100.0, 1.0), Controls(), entry_price=100_200.0)
    assert "entry_near_market" in {c.name for c in bad.failures}


# ---- per-position exits (trend trades) ----------------------------------------------------

def pos(**kw):
    base = dict(coin="BTC", qty=1.0, entry_price=100.0, stop_loss=91.0, take_profit=1100.0,
                opened_at=T0, highest_price=100.0, trail_pct=9.0, trailing_active=True, max_hold_hours=24 * 120)
    base.update(kw)
    return Position(**base)


def test_position_trailing_distance_overrides_config(config):
    p = pos(highest_price=120.0)
    r = check_exit(p, 110.0, T0 + timedelta(days=5), config.exits)
    assert r.action == "HOLD" and r.effective_stop == pytest.approx(120 * 0.91)
    assert check_exit(p, 109.0, T0 + timedelta(days=5), config.exits).reason == "trailing_stop"


def test_position_max_hold_overrides_config(config):
    p = pos()
    assert check_exit(p, 100.0, T0 + timedelta(hours=25), config.exits).action == "HOLD"   # config says 24h
    assert check_exit(p, 100.0, T0 + timedelta(days=121), config.exits).reason == "max_hold"


# ---- strategy settings ---------------------------------------------------------------------

def test_swing_mode_overrides_scanner_and_exits(config):
    cfg = apply_strategy(with_(config, strategy={"mode": "swing"}))
    s = config.strategy.swing
    assert cfg.scanner.setup_timeframe == s.setup_timeframe and cfg.scanner.trend_timeframe == s.trend_timeframe
    assert cfg.exits.max_hold_hours == s.max_hold_hours and cfg.scanner.target_atr_multiple == s.target_atr_multiple
    assert s.trend_timeframe in cfg.data.timeframes


def test_trend_mode_widens_stop_limit(config):
    cfg = apply_strategy(with_(config, strategy={"mode": "trend"}))
    assert cfg.risk.max_stop_distance_pct == config.strategy.trend.max_stop_distance_pct
    assert apply_strategy(config) is config                     # day mode: unchanged


def test_bot_refuses_modes_it_cannot_trade_yet(config):
    assert paper_bot_supports(config) is None
    assert paper_bot_supports(with_(config, strategy={"mode": "trend"})) is None       # trend runs in the bot
    assert "backtest-only" in paper_bot_supports(with_(config, strategy={"mode": "swing"}))
    assert "limit orders" in paper_bot_supports(with_(config, execution={"entry_order": "limit"}))


# ---- the daily trend rule ---------------------------------------------------------------------

def daily(prices):
    idx = pd.date_range("2025-01-01", periods=len(prices), freq="1D", tz="UTC")
    c = np.array(prices, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0}, index=idx)


def test_trend_breakout_uses_only_previous_days(config):
    t = config.strategy.trend
    prices = [100.0] * 80 + [110.0]                              # flat, then a breakout day
    f = trend_frame(daily(prices), t)
    assert f["prior_high"].iloc[-1] == pytest.approx(101.0)      # the breakout day's own high isn't included
    sig = trend_signal("BTC", f.iloc[-1], t)
    assert sig is not None and sig.kind == "trend_breakout"
    assert sig.trail_pct == pytest.approx(t.stop_atr_multiple * f["atr_pct"].iloc[-1])
    assert sig.target_pct == NO_TARGET_PCT and sig.max_hold_hours == t.max_hold_days * 24
    assert trend_signal("BTC", f.iloc[-2], t) is None            # the day before: no breakout


def test_no_trend_signal_in_a_downtrend(config):
    prices = [200.0 - i for i in range(80)] + [125.0]           # bounce, but still below the 50-day average
    f = trend_frame(daily(prices), config.strategy.trend)
    assert trend_signal("BTC", f.iloc[-1], config.strategy.trend) is None


def test_resample_infers_hourly_base():
    df = candles(n=48, freq="1h")
    d = resample(df, "1d")
    assert len(d) == 2 and d["volume"].iloc[0] == 240.0


# ---- limit orders inside a candle ---------------------------------------------------------------

def test_resting_take_profit_fills_at_the_target_only_when_traded_through(config):
    p = pos(trail_pct=None, trailing_active=False, stop_loss=98.0, take_profit=104.0, max_hold_hours=None)
    touched = bar_exit(p, 101.0, 104.0, 100.5, 103.0, T0, config.exits, 0.0, tp_limit=True)
    assert touched.reason is None                                # touching isn't enough: someone must pay more
    through = bar_exit(p, 101.0, 104.2, 100.5, 103.0, T0, config.exits, 0.001, tp_limit=True)
    assert through.reason == "take_profit" and through.price == 104.0 and through.exact


def test_fill_candle_checks_only_the_stop(config):
    p = pos(trail_pct=None, trailing_active=False, stop_loss=98.0, take_profit=101.0, max_hold_hours=None)
    assert bar_exit(p, 100, 102, 99, 100.5, T0, config.exits, 0.0, stop_only=True).reason is None
    assert bar_exit(p, 100, 102, 97, 100.5, T0, config.exits, 0.0, stop_only=True).reason == "stop_loss"


# ---- the engine with limit orders ---------------------------------------------------------------------

def run(config, monkeypatch, price, fire_at, *, entry="limit", tp="limit", wick=0.002, **risk):
    start, end = T0 + timedelta(days=2), T0 + timedelta(days=3)
    df = candles(n=3 * 288, price=price, wick=wick)
    fire = set(fire_at)

    def fake_detect(t, s, g, sc):
        return [Setup("trend_pullback", ["forced"], 1.0, 2.0)] if s.candle_close_time in fire else []

    monkeypatch.setattr(engine, "detect_setups", fake_detect)
    cfg = with_(config, execution={"entry_order": entry, "take_profit_order": tp}, risk=risk or {})
    bt = Backtester(cfg, {"BTC": df}, [config.coin("BTC")], start, end, spread_pct=0.2, slippage_pct=0.05)
    return bt.run(), df


def iso(t):
    from desk.clock import iso as _iso
    return _iso(t)


def test_limit_buy_fills_at_the_bid_only_after_a_dip(config, monkeypatch):
    signal = T0 + timedelta(days=2, hours=1)
    k = int((signal - T0).total_seconds() // 300)
    # price 100, dips to 99.5 two candles after the signal, then rallies past the target
    r, _ = run(config, monkeypatch, lambda i: 99.5 if i == k + 2 else (100.0 if i < k + 6 else 103.0),
               {iso(signal)})
    t = r.trades.iloc[0]
    assert t["entry_price"] == pytest.approx(100.0 * (1 - 0.001))        # the bid at the signal, no slippage
    assert t["exit_reason"] == "take_profit"
    assert t["exit_price"] == pytest.approx(t["entry_price"] * 1.02)     # resting sell at exactly the target


def test_limit_buy_cancelled_if_price_runs_away(config, monkeypatch):
    signal = T0 + timedelta(days=2, hours=1)
    k = int((signal - T0).total_seconds() // 300)
    # no wicks: after the signal the price only rises, so nobody ever sells down to our bid
    r, _ = run(config, monkeypatch, lambda i: 100.0 if i <= k else 100.0 * (1 + 0.003 * (i - k)), {iso(signal)},
               wick=0.0)
    assert r.trades.empty and r.open_at_end == 0
    assert r.skipped["limit buy not filled (cancelled)"] == 1


def test_limit_and_market_costs_compared(config, monkeypatch):
    signal = T0 + timedelta(days=2, hours=1)
    k = int((signal - T0).total_seconds() // 300)
    path = lambda i: 99.5 if i == k + 2 else (100.0 if i < k + 6 else 103.0)   # noqa: E731
    lim, _ = run(config, monkeypatch, path, {iso(signal)})
    mkt, _ = run(config, monkeypatch, path, {iso(signal)}, entry="market", tp="market")
    a, b = lim.trades.iloc[0], mkt.trades.iloc[0]
    assert a["entry_price"] < b["entry_price"] * (1 - 0.002)       # bought at the bid, not the ask + slippage
    assert a["pnl_pct"] > b["pnl_pct"]                               # and kept more of the move


# ---- the engine in trend mode (hourly candles, daily decisions) ----------------------------------------

def test_trend_mode_end_to_end(config):
    # 100 days flat, then a steady uptrend for 40 days, then a sharp fall
    hours = 24 * 160

    def price(i):
        d = i / 24
        if d < 100:
            return 100.0 + (i % 5) * 0.01
        if d < 140:
            return 100.0 * (1.01 ** (d - 100))
        return 100.0 * (1.01 ** 40) * (0.97 ** (d - 140))

    df = candles(start=T0, n=hours, price=price, freq="1h")
    cfg = with_(config, strategy={"mode": "trend"})
    bt = Backtester(cfg, {"BTC": df}, [config.coin("BTC")], T0 + timedelta(days=90), T0 + timedelta(days=160),
                    base_tf="1h", spread_pct=0.2, slippage_pct=0.05)
    r = bt.run()
    assert len(r.trades) >= 1
    first = r.trades.iloc[0]
    opened = pd.Timestamp(first["opened"])
    assert opened.hour == 0 and opened >= pd.Timestamp(T0 + timedelta(days=100))   # decided at a daily close
    assert first["exit_reason"] == "trailing_stop" and first["pnl_pct"] > 10    # rode the trend, exited on the fall
    assert r.settings["mode"] == "trend" and r.settings["base_timeframe"] == "1h"
