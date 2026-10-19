import json
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest
import respx

import desk.backtest.engine as engine
from desk.backtest.engine import BacktestBroker, Backtester, TfSeries, bar_exit
from desk.backtest.history import get_history, load_cache, resample, save_cache
from desk.backtest.report import drawdown_series, max_drawdown_pct, metrics, write_report
from desk.broker import BrokerError
from desk.models import Position
from desk.scanner import Setup

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def candles(start=T0, n=600, price=lambda i: 100.0, unit="ns", spread=0.002):
    idx = pd.date_range(start, periods=n, freq="5min", tz="UTC").as_unit(unit)
    close = np.array([price(i) for i in range(n)], dtype=float)
    opn = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": opn, "high": np.maximum(opn, close) * (1 + spread),
                         "low": np.minimum(opn, close) * (1 - spread), "close": close,
                         "volume": np.full(n, 10.0)}, index=pd.DatetimeIndex(idx, name="open_time"))


# ---- history ---------------------------------------------------------------------

def test_resample_builds_exact_candles_and_drops_incomplete():
    df = candles(n=7, price=lambda i: 100.0 + i)          # 7 x 5m = two full 15m buckets + 1 partial
    r = resample(df, "15m")
    assert len(r) == 2
    first = r.iloc[0]
    assert first["open"] == df["open"].iloc[0] and first["close"] == df["close"].iloc[2]
    assert first["high"] == df["high"].iloc[:3].max() and first["volume"] == 30.0


def test_cache_roundtrip(tmp_path):
    df = candles(n=20)
    save_cache(df, tmp_path / "x.csv")
    back = load_cache(tmp_path / "x.csv")
    assert len(back) == 20 and back.index.tz is not None
    assert back.index.unit == "ns"
    assert np.allclose(back["close"], df["close"])


def binance_rows(start, n):
    rows = []
    for i in range(n):
        ms = int((start + timedelta(minutes=5 * i)).timestamp() * 1000)
        rows.append([ms, "100", "101", "99", "100.5", "1", ms + 299_999, "0", 1, "0", "0", "0"])
    return rows


@respx.mock
def test_get_history_downloads_once_then_only_the_missing_tail(tmp_path, config):
    coin = config.coin("BTC")
    now = T0 + timedelta(days=10)
    route = respx.get("https://data-api.binance.vision/api/v3/klines").mock(
        side_effect=lambda req: httpx.Response(200, json=binance_rows(
            datetime.fromtimestamp(int(req.url.params["startTime"]) / 1000, UTC),
            min(1000, (int(req.url.params["endTime"]) + 1 - int(req.url.params["startTime"])) // 300_000))))
    with httpx.Client() as http:
        df = get_history(http, coin, T0, T0 + timedelta(hours=10), tmp_path, now)
        assert len(df) == 120 and route.call_count == 1
        df2 = get_history(http, coin, T0, T0 + timedelta(hours=12), tmp_path, now)
        assert len(df2) == 144 and route.call_count == 2           # only the 2 new hours downloaded
        df3 = get_history(http, coin, T0, T0 + timedelta(hours=12), tmp_path, now, offline=True)
        assert len(df3) == 144 and route.call_count == 2


@respx.mock
def test_download_pages_through_long_ranges(tmp_path, config):
    coin = config.coin("ETH")
    now = T0 + timedelta(days=30)
    respx.get("https://data-api.binance.vision/api/v3/klines").mock(
        side_effect=lambda req: httpx.Response(200, json=binance_rows(
            datetime.fromtimestamp(int(req.url.params["startTime"]) / 1000, UTC),
            min(1000, (int(req.url.params["endTime"]) + 1 - int(req.url.params["startTime"])) // 300_000))))
    with httpx.Client() as http:
        df = get_history(http, coin, T0, T0 + timedelta(days=5), tmp_path, now)   # 1440 candles = 2 pages
    assert len(df) == 1440 and df.index.is_monotonic_increasing and not df.index.has_duplicates


# ---- no look-ahead ---------------------------------------------------------------------

@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_views_never_use_unclosed_candles(config, unit):
    df = candles(n=24 * 12 * 3, price=lambda i: 100.0 + i, unit=unit)     # 3 days, price = 100 + candle number
    s15 = TfSeries.build(df, "15m", config)
    t = T0 + timedelta(hours=10)                                           # exactly a 15m close
    view, closed = s15.view_at(t)
    assert closed == t                                                     # the candle closing AT t counts
    assert view.close == df["close"].iloc[(10 * 12) - 1]                   # its close = last 5m candle before t
    view2, closed2 = s15.view_at(t - timedelta(seconds=1))
    assert closed2 == t - timedelta(minutes=15)                            # one second earlier: previous candle
    assert s15.view_at(T0)[0] is None                                      # nothing closed yet


# ---- exits inside a candle --------------------------------------------------------------

@pytest.fixture
def ex(config):
    return config.exits


def pos(**kw):
    base = dict(coin="BTC", qty=1.0, entry_price=100.0, stop_loss=98.0, take_profit=104.0,
                opened_at=T0, highest_price=100.0)
    base.update(kw)
    return Position(**base)


def test_bar_exit_stop(ex):
    r = bar_exit(pos(), 99.5, 100.0, 97.5, 98.5, T0 + timedelta(minutes=5), ex, 0.0)
    assert r.reason == "stop_loss" and r.price == 98.0


def test_bar_exit_gap_below_stop_fills_at_open(ex):
    r = bar_exit(pos(), 96.0, 96.5, 95.0, 96.0, T0 + timedelta(minutes=5), ex, 0.0)
    assert r.reason == "stop_loss" and r.price == 96.0


def test_bar_exit_both_touched_assumes_stop_first(ex):
    r = bar_exit(pos(), 100.0, 105.0, 97.0, 101.0, T0 + timedelta(minutes=5), ex, 0.0)
    assert r.reason == "stop_loss"


def test_bar_exit_take_profit_and_gap_up(ex):
    assert bar_exit(pos(), 101.0, 104.5, 100.5, 104.0, T0, ex, 0.0).price == 104.0
    assert bar_exit(pos(), 106.0, 107.0, 105.0, 106.5, T0, ex, 0.0).price == 106.0


def test_bar_exit_trailing_updates_for_next_candle(ex):
    r = bar_exit(pos(), 100.5, 102.0, 100.4, 101.8, T0, ex, 0.0)
    assert r.reason is None and r.trailing and r.highest == 102.0
    nxt = bar_exit(pos(highest_price=r.highest, trailing_active=True), 101.5, 101.6, 101.0, 101.2, T0, ex, 0.0)
    assert nxt.reason == "trailing_stop" and nxt.price == pytest.approx(102.0 * 0.992)


def test_bar_exit_uses_bid_side(ex):
    # half spread 1%: a low of 98.9 is a bid of ~97.9 -> stopped
    assert bar_exit(pos(), 100, 100, 98.9, 99.5, T0, ex, 0.01).reason == "stop_loss"


def test_bar_exit_max_hold(ex):
    r = bar_exit(pos(), 100, 100.5, 99.5, 100.2, T0 + timedelta(hours=24), ex, 0.0)
    assert r.reason == "max_hold" and r.price == 100.2


# ---- fills ----------------------------------------------------------------------------

def test_backtest_broker():
    b = BacktestBroker(fee_pct=0.1, slippage_pct=0.05)
    b.set_quote("BTC", 99.0, 101.0)
    f = b.buy("BTC", 2.0)
    assert f.avg_price == pytest.approx(101.0 * 1.0005) and f.fee_aud == pytest.approx(f.notional_aud * 0.001)
    assert b.sell("BTC", 1.0).avg_price == pytest.approx(99.0 * 0.9995)
    with pytest.raises(BrokerError):
        b.buy("BTC", 1.0, max_price=101.0)


# ---- the engine -------------------------------------------------------------------------

def run(config, monkeypatch, price, fire_at=(), n_days=4, **overrides):
    start = T0 + timedelta(days=2)
    end = T0 + timedelta(days=n_days)
    df = candles(n=int(n_days * 288), price=price)
    fire = set(fire_at)

    def fake_detect(t, s, g, sc):
        return [Setup("trend_pullback", ["forced"], 1.0, 2.0)] if s.candle_close_time in fire else []

    monkeypatch.setattr(engine, "detect_setups", fake_detect)
    cfg = config.model_copy(update={"risk": config.risk.model_copy(update=overrides)}) if overrides else config
    bt = Backtester(cfg, {"BTC": df}, [config.coin("BTC")], start, end, spread_pct=0.0, slippage_pct=0.0)
    return bt, bt.run(), df


def iso_at(t):
    from desk.clock import iso
    return iso(t)


def test_no_setups_means_no_trades_and_flat_equity(config, monkeypatch):
    _, r, _ = run(config, monkeypatch, price=lambda i: 100.0 + (i % 7))
    assert r.trades.empty and r.fees_aud == 0
    assert r.equity.iloc[-1] == pytest.approx(r.starting_balance)


def test_entry_fills_at_next_candle_open_then_stops_out(config, monkeypatch):
    signal = T0 + timedelta(days=2, hours=1)                     # a 15m close
    crash = int((signal - T0).total_seconds() // 300) + 6         # price drops 6 candles later

    def price(i):
        return 100.0 if i < crash else 90.0

    bt, r, df = run(config, monkeypatch, price, fire_at={iso_at(signal)}, min_edge_multiple=1.0)
    assert len(r.trades) == 1
    t = r.trades.iloc[0]
    assert pd.Timestamp(t["opened"]) == pd.Timestamp(signal)       # filled at the open right after the signal
    assert t["entry_price"] == pytest.approx(df.loc[pd.Timestamp(signal), "open"])
    assert t["exit_reason"] == "stop_loss" and t["pnl_aud"] < 0
    # the crash happened INSIDE a candle that opened above the stop, so the fill is the stop itself
    assert t["exit_price"] == pytest.approx(t["entry_price"] * 0.99)


def test_take_profit_trade(config, monkeypatch):
    signal = T0 + timedelta(days=2, hours=1)
    jump = int((signal - T0).total_seconds() // 300) + 3

    _, r, _ = run(config, monkeypatch, lambda i: 100.0 if i < jump else 103.0,
                  fire_at={iso_at(signal)}, min_edge_multiple=1.0)
    assert r.trades.iloc[0]["exit_reason"] == "take_profit" and r.trades.iloc[0]["pnl_aud"] > 0
    m = metrics(r)
    assert m["win_rate_pct"] == 100 and m["trades"] == 1 and m["fees_aud"] > 0


def test_risk_manager_still_applies(config, monkeypatch):
    signal = T0 + timedelta(days=2, hours=1)
    # with the default 3x cost rule and a 0.2% spread, a 2% target passes; with min_edge_multiple 50 it can't
    bt, r, _ = run(config, monkeypatch, lambda i: 100.0, fire_at={iso_at(signal)}, min_edge_multiple=50.0)
    assert r.trades.empty and r.risk_rejections["beats_costs"] == 1


def test_max_trades_per_day_respected(config, monkeypatch):
    # a setup every hour, and the price steps up 3% every hour so each trade hits its +2% target fast;
    # with max_entries_per_day=1 only the first setup of each local day may trade
    fires = {iso_at(T0 + timedelta(days=2, hours=h)) for h in range(0, 48)}
    _, r, _ = run(config, monkeypatch, lambda i: 100.0 * 1.03 ** (i // 12), fire_at=fires,
                  min_edge_multiple=1.0, max_entries_per_day=1)
    opened_days = list(pd.to_datetime(r.trades["opened"]).dt.tz_convert(config.tz).dt.date)
    assert len(opened_days) >= 2                                  # the window spans 3 local days
    assert len(set(opened_days)) == len(opened_days)              # never two in one day
    assert r.skipped["max trades per day"] > 0


# ---- metrics & report ------------------------------------------------------------------

def test_drawdown_maths():
    s = pd.Series([100.0, 120.0, 90.0, 130.0, 117.0])
    assert max_drawdown_pct(s) == pytest.approx(-25.0)
    assert list(drawdown_series(s).round(2)) == [0.0, 0.0, -25.0, 0.0, -10.0]
    assert max_drawdown_pct(pd.Series([1.0, 2.0, 3.0])) == 0.0


def test_hold_benchmark_tracks_btc(config, monkeypatch):
    _, r, _ = run(config, monkeypatch, lambda i: 100.0 * (1 + i / 1152))    # price roughly doubles
    gross = r.hold.iloc[-1] / r.starting_balance
    first_open, last_close = 100 * (1 + 575 / 1152), 100 * (1 + 1151 / 1152)   # replay starts on day 2
    assert gross == pytest.approx(last_close / first_open * 0.999 / 1.001, rel=1e-9)   # minus 2 fees
    assert metrics(r)["hold_return_pct"] == pytest.approx((gross - 1) * 100)


def test_report_files(config, monkeypatch, tmp_path):
    signal = T0 + timedelta(days=2, hours=1)
    jump = int((signal - T0).total_seconds() // 300) + 3
    _, r, _ = run(config, monkeypatch, lambda i: 100.0 if i < jump else 103.0,
                  fire_at={iso_at(signal)}, min_edge_multiple=1.0)
    m = write_report(r, tmp_path / "rep")
    for name in ("report.html", "summary.txt", "trades.csv", "equity_hourly.csv", "metrics.json"):
        assert (tmp_path / "rep" / name).exists(), name
    html = (tmp_path / "rep" / "report.html").read_text(encoding="utf-8")
    assert "Just hold BTC" in html and "What this test can and can't tell you" in html
    assert json.loads((tmp_path / "rep" / "metrics.json").read_text())["trades"] == m["trades"] == 1
