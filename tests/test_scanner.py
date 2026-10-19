from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from desk.clock import FakeClock
from desk.config import TIMEFRAME_SECONDS
from desk.data.candles import CandleError, CandleSet
from desk.data.market import MarketSnapshot
from desk.exchange.coinspot import Quote
from desk.scanner import Scanner, TfView, classify_trend, compute_view, detect_setups, position_alerts

NEUTRAL = TfView(
    timeframe="x", candle_close_time="", close=100.0, prev_close=100.0,
    ema_fast=100.0, ema_mid=100.0, ema_slow=100.0, ema_trend=100.0,
    rsi=50.0, prev_rsi=50.0, min_rsi_recent=50.0,
    macd=0.0, macd_signal=0.0, hist=0.0, prev_hist=0.0,
    atr=1.0, atr_pct=1.0, vol_ratio=1.0, breakout_level=105.0,
)


def v(tf, **kw):
    return replace(NEUTRAL, timeframe=tf, **kw)


UPTREND = v("1h", close=110, ema_slow=105, ema_trend=100)
RISING = v("5m", hist=0.10, prev_hist=0.05)


@pytest.fixture
def sc(config):
    return config.scanner


# ---- trend -------------------------------------------------------------------

def test_classify_trend():
    assert classify_trend(UPTREND) == "up"
    assert classify_trend(v("1h", close=90, ema_slow=95, ema_trend=100)) == "down"
    assert classify_trend(v("1h", close=104, ema_slow=105, ema_trend=100)) == "flat"
    assert classify_trend(v("1h", ema_trend=None)) == "unknown"


# ---- trend pullback -------------------------------------------------------------

PULLBACK = v("15m", close=100.2, ema_mid=100.0, atr=1.0, atr_pct=1.0, min_rsi_recent=36, rsi=42, prev_rsi=38)


def kinds(t, s, g, sc):
    return [x.kind for x in detect_setups(t, s, g, sc)]


def test_trend_pullback_detected(sc):
    setups = detect_setups(UPTREND, PULLBACK, RISING, sc)
    assert [x.kind for x in setups] == ["trend_pullback"]
    st = setups[0]
    assert st.suggested_stop_pct == pytest.approx(1.0 * sc.stop_atr_multiple)
    assert st.suggested_target_pct == pytest.approx(1.0 * sc.target_atr_multiple)
    assert any("RSI dipped to 36.0" in r for r in st.reasons)


@pytest.mark.parametrize("trend,setup,trigger", [
    (v("1h", close=104, ema_slow=105, ema_trend=100), PULLBACK, RISING),          # no uptrend
    (UPTREND, replace(PULLBACK, min_rsi_recent=45), RISING),                      # RSI never dipped
    (UPTREND, replace(PULLBACK, rsi=37, prev_rsi=38), RISING),                    # RSI still falling
    (UPTREND, replace(PULLBACK, close=101.0), RISING),                            # too far above EMA (>0.75 ATR)
    (UPTREND, PULLBACK, v("5m", hist=0.04, prev_hist=0.05)),                       # trigger momentum falling
])
def test_trend_pullback_rejected(sc, trend, setup, trigger):
    assert "trend_pullback" not in kinds(trend, setup, trigger, sc)


def test_disabled_setup_not_reported(sc):
    sc2 = sc.model_copy(update={"setups": ["volume_breakout"]})
    assert kinds(UPTREND, PULLBACK, RISING, sc2) == []


# ---- volume breakout -------------------------------------------------------------

BREAKOUT = v("15m", close=106, breakout_level=105, vol_ratio=2.5, hist=0.2, prev_hist=0.1, rsi=65, prev_rsi=60)


def test_volume_breakout_detected(sc):
    assert "volume_breakout" in kinds(UPTREND, BREAKOUT, NEUTRAL, sc)


@pytest.mark.parametrize("setup,trend", [
    (replace(BREAKOUT, vol_ratio=1.5), UPTREND),               # no volume spike
    (replace(BREAKOUT, rsi=80), UPTREND),                      # overbought
    (replace(BREAKOUT, close=104.9), UPTREND),                 # didn't break the high
    (replace(BREAKOUT, hist=-0.1, prev_hist=-0.2), UPTREND),   # momentum negative
    (BREAKOUT, v("1h", close=100, ema_slow=105, ema_trend=110)),   # downtrend
])
def test_volume_breakout_rejected(sc, setup, trend):
    assert "volume_breakout" not in kinds(trend, setup, NEUTRAL, sc)


# ---- oversold bounce --------------------------------------------------------------

def test_oversold_bounce_via_trigger_cross(sc):
    g = v("5m", prev_rsi=28, rsi=32, hist=0.1, prev_hist=0.0)
    assert "oversold_bounce" in kinds(UPTREND, NEUTRAL, g, sc)


def test_oversold_bounce_via_setup_rsi(sc):
    s = v("15m", rsi=26, prev_rsi=24)
    assert "oversold_bounce" in kinds(UPTREND, s, RISING, sc)


def test_oversold_bounce_needs_long_term_uptrend(sc):
    t = v("1h", close=95, ema_slow=97, ema_trend=100)
    g = v("5m", prev_rsi=28, rsi=32, hist=0.1, prev_hist=0.0)
    assert "oversold_bounce" not in kinds(t, NEUTRAL, g, sc)


def test_oversold_bounce_needs_rising_momentum(sc):
    g = v("5m", prev_rsi=28, rsi=32, hist=-0.1, prev_hist=0.0)
    assert "oversold_bounce" not in kinds(UPTREND, NEUTRAL, g, sc)


def test_no_atr_means_no_setups(sc):
    assert detect_setups(UPTREND, replace(PULLBACK, atr_pct=None), RISING, sc) == []


def test_neutral_market_finds_nothing(sc):
    assert detect_setups(NEUTRAL, NEUTRAL, NEUTRAL, sc) == []


# ---- alerts ---------------------------------------------------------------------

def test_position_alerts(sc):
    s = v("15m", hist=-0.1, prev_hist=0.1, close=99, prev_close=101, ema_mid=100, rsi=50)
    t = v("1h", close=100, ema_slow=105)
    g = v("5m", vol_ratio=3.0, close=98, prev_close=99)
    alerts = position_alerts(t, s, g, sc)
    assert len(alerts) == 4
    assert any("MACD crossed below" in a for a in alerts)
    assert any("fell below EMA21" in a for a in alerts)
    assert any("trend weakening" in a for a in alerts)
    assert any("selling on a volume spike" in a for a in alerts)
    assert position_alerts(UPTREND, NEUTRAL, NEUTRAL, sc) == []


# ---- full scanner with fake data -------------------------------------------------

NOW = datetime(2026, 10, 9, 8, 0, 30, tzinfo=UTC)


def candles(tf, n=300, growth=1.0005, end=NOW, noise=0.0, seed=0):
    step = TIMEFRAME_SECONDS[tf]
    last_open = datetime.fromtimestamp((int(end.timestamp()) // step) * step - step, UTC)
    idx = pd.DatetimeIndex([last_open - timedelta(seconds=step * (n - 1 - i)) for i in range(n)])
    rng = np.random.default_rng(seed)
    close = 100 * growth ** np.arange(n) * (1 + rng.normal(0, noise, n))
    df = pd.DataFrame({"open": close * 0.999, "high": close * 1.002, "low": close * 0.997, "close": close,
                       "volume": np.full(n, 10.0)}, index=idx)
    return df


class FakeFeed:
    def __init__(self, frames, spot=(70.0, "USDT", "binance"), fail=()):
        self.frames = frames
        self.spot = spot
        self.fail = set(fail)

    def get(self, coin, tf, limit):
        if tf in self.fail:
            raise CandleError(f"No candles for {coin.code} {tf}: test")
        return CandleSet(coin.code, tf, self.frames[tf], "binance", "USDT", NOW)

    def spot_price(self, coin):
        return self.spot


def snapshot(mid=100.0, at=NOW, with_usdt=True):
    q = Quote("BTC", mid * 0.999, mid * 1.001, mid, at)
    usdt = Quote("USDT", 1.428, 1.4291, 1.4286, at) if with_usdt else None
    return MarketSnapshot(quotes={"BTC": q}, usdt_aud=usdt, fetched_at=at)


def make_scanner(config, feed, db=None):
    return Scanner(config, feed, FakeClock(NOW, "Australia/Sydney"), db)


def test_compute_view_on_uptrend(config):
    df = candles("1h")
    view = compute_view(CandleSet("BTC", "1h", df, "binance", "USDT", NOW), config.scanner)
    assert classify_trend(view) == "up"
    assert 0 < view.atr_pct < 1
    assert view.rsi > 50


def test_scan_healthy_coin(config, db):
    frames = {tf: candles(tf) for tf in config.data.timeframes}
    feed = FakeFeed(frames, spot=(70.0, "USDT", "binance"))   # 70 USDT * 1.4286 = 100.0 AUD
    r = make_scanner(config, feed, db).scan(snapshot(100.0))[0]
    assert r.coin == "BTC"
    assert r.healthy, r.problems
    assert r.trend == "up"
    assert abs(r.global_divergence_pct) < 0.1
    rows = db.query("SELECT * FROM scanner_signals")
    assert len(rows) == 3          # one per configured coin (ETH/SOL have no price -> problems)
    btc = [x for x in rows if x["coin"] == "BTC"][0]
    assert btc["healthy"] == 1


def test_scan_reports_data_problems(config):
    frames = {tf: candles(tf) for tf in config.data.timeframes}
    frames["15m"] = candles("15m", end=NOW - timedelta(hours=2))          # stale
    feed = FakeFeed(frames, spot=(80.0, "USDT", "binance"), fail={"5m"})  # 80 USDT = 114 AUD -> divergence
    r = make_scanner(config, feed).scan_coin(config.coin("BTC"), snapshot(100.0, at=NOW - timedelta(minutes=5)))
    joined = " | ".join(r.problems)
    assert "5m candles unavailable" in joined
    assert "15m candles are stale" in joined
    assert "from the global price" in joined
    assert "CoinSpot price is 300s old" in joined
    assert not r.healthy and not r.flagged


def test_setups_suppressed_when_data_unhealthy(config, monkeypatch):
    import desk.scanner as scanner_mod
    from desk.scanner import Setup
    monkeypatch.setattr(scanner_mod, "detect_setups", lambda *a: [Setup("trend_pullback", ["x"], 1.0, 2.0)])
    frames = {tf: candles(tf) for tf in config.data.timeframes}
    good = make_scanner(config, FakeFeed(frames)).scan_coin(config.coin("BTC"), snapshot(100.0))
    assert good.flagged and good.setups
    bad = make_scanner(config, FakeFeed(frames, spot=(90.0, "USDT", "binance"))).scan_coin(
        config.coin("BTC"), snapshot(100.0))
    assert not bad.flagged and bad.suppressed_setups and bad.setups == []


def test_missing_usdt_rate_is_a_problem(config):
    frames = {tf: candles(tf) for tf in config.data.timeframes}
    r = make_scanner(config, FakeFeed(frames)).scan_coin(config.coin("BTC"), snapshot(100.0, with_usdt=False))
    assert any("USDT" in p for p in r.problems)


def test_one_coin_crashing_does_not_stop_others(config, monkeypatch):
    frames = {tf: candles(tf) for tf in config.data.timeframes}
    s = make_scanner(config, FakeFeed(frames))
    real = s.scan_coin

    def boom(coin, snap):
        if coin.code == "ETH":
            raise RuntimeError("kaboom")
        return real(coin, snap)

    monkeypatch.setattr(s, "scan_coin", boom)
    results = s.scan(snapshot(100.0))
    assert [r.coin for r in results] == ["BTC", "ETH", "SOL"]
    assert "scanner error: kaboom" in results[1].problems[0]
