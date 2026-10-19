from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest
import respx

from desk.clock import FakeClock
from desk.config import CoinConfig
from desk.data.candles import (
    BinanceProvider,
    CandleError,
    CandleFeed,
    KrakenProvider,
    validate,
)

BTC = CoinConfig(code="BTC", binance="BTCUSDT", kraken="XBTAUD")
NOW = datetime(2026, 10, 9, 8, 7, 30, tzinfo=UTC)   # mid-way through the 08:05 5m candle


def binance_rows(n: int, end_open: datetime, step_s: int = 300):
    rows = []
    for i in range(n):
        o = end_open - timedelta(seconds=step_s * (n - 1 - i))
        ms = int(o.timestamp() * 1000)
        p = 100 + i
        rows.append([ms, str(p), str(p + 2), str(p - 1), str(p + 1), "10.5", ms + step_s * 1000 - 1,
                     "0", 5, "0", "0", "0"])
    return rows


def test_binance_parse_drops_forming_candle():
    rows = binance_rows(5, end_open=datetime(2026, 10, 9, 8, 5, tzinfo=UTC))
    df = BinanceProvider.parse(rows, NOW)
    assert len(df) == 4                                   # 08:05 candle still forming
    assert df.index[-1] == pd.Timestamp("2026-10-09 08:00", tz="UTC")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].dtype == float


def test_kraken_parse_drops_forming_candle_and_uses_volume_column():
    base = int(datetime(2026, 10, 9, 7, 50, tzinfo=UTC).timestamp())
    rows = [[base + 300 * i, "10", "12", "9", "11", "10.5", "3.25", 4] for i in range(4)]  # 07:50 .. 08:05
    df = KrakenProvider.parse({"XBTAUD": rows, "last": 123}, "5m", NOW)
    assert len(df) == 3
    assert df["volume"].iloc[0] == 3.25


def test_validate_rejects_bad_candles():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-09 08:00", tz="UTC"), pd.Timestamp("2026-10-09 08:05", tz="UTC")])
    good = pd.DataFrame({"open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.5], "close": [1.5, 2.5],
                         "volume": [1.0, 1.0]}, index=idx)
    validate(good, "5m")
    with pytest.raises(CandleError, match="out of order"):
        validate(good.iloc[::-1], "5m")
    bad = good.copy()
    bad.loc[bad.index[0], "high"] = 0.1
    with pytest.raises(CandleError, match="high/low"):
        validate(bad, "5m")
    nan = good.copy()
    nan.loc[nan.index[1], "close"] = float("nan")
    with pytest.raises(CandleError, match="missing"):
        validate(nan, "5m")
    with pytest.raises(CandleError, match="no candles"):
        validate(good.iloc[0:0], "5m")


@respx.mock
def test_binance_falls_back_to_second_host():
    clock = FakeClock(NOW)
    respx.get("https://data-api.binance.vision/api/v3/klines").mock(return_value=httpx.Response(451))
    respx.get("https://api.binance.com/api/v3/klines").mock(
        return_value=httpx.Response(200, json=binance_rows(10, datetime(2026, 10, 9, 8, 5, tzinfo=UTC))))
    df = BinanceProvider(httpx.Client(), clock).fetch(BTC, "5m", 5)
    assert len(df) == 5


@respx.mock
def test_feed_falls_back_to_kraken_and_reports_provider():
    clock = FakeClock(NOW)
    respx.get(url__startswith="https://data-api.binance.vision").mock(return_value=httpx.Response(500))
    respx.get(url__startswith="https://api.binance.com").mock(side_effect=httpx.ConnectError("down"))
    base = int(datetime(2026, 10, 9, 7, 0, tzinfo=UTC).timestamp())
    rows = [[base + 300 * i, "10", "12", "9", "11", "10.5", "3", 4] for i in range(14)]
    respx.get("https://api.kraken.com/0/public/OHLC").mock(
        return_value=httpx.Response(200, json={"error": [], "result": {"XBTAUD": rows, "last": 1}}))
    http = httpx.Client()
    feed = CandleFeed([BinanceProvider(http, clock), KrakenProvider(http, clock)], clock)
    cs = feed.get(BTC, "5m", 10)
    assert cs.provider == "kraken" and cs.quote_currency == "AUD"
    assert len(cs.df) == 10


@respx.mock
def test_feed_raises_when_all_providers_fail():
    clock = FakeClock(NOW)
    respx.get(url__startswith="https://data-api.binance.vision").mock(return_value=httpx.Response(500))
    respx.get(url__startswith="https://api.binance.com").mock(return_value=httpx.Response(500))
    feed = CandleFeed([BinanceProvider(httpx.Client(), clock)], clock)
    with pytest.raises(CandleError, match="No candles for BTC"):
        feed.get(BTC, "5m", 10)


@respx.mock
def test_feed_caches_briefly():
    clock = FakeClock(NOW)
    t = [0.0]
    route = respx.get("https://data-api.binance.vision/api/v3/klines").mock(
        return_value=httpx.Response(200, json=binance_rows(10, datetime(2026, 10, 9, 8, 5, tzinfo=UTC))))
    feed = CandleFeed([BinanceProvider(httpx.Client(), clock)], clock, cache_seconds=30, monotonic=lambda: t[0])
    feed.get(BTC, "5m", 5)
    feed.get(BTC, "5m", 5)
    assert route.call_count == 1
    t[0] = 31
    feed.get(BTC, "5m", 5)
    assert route.call_count == 2


def test_freshness():
    clock = FakeClock(NOW)
    df = BinanceProvider.parse(binance_rows(5, datetime(2026, 10, 9, 8, 5, tzinfo=UTC)), NOW)
    from desk.data.candles import CandleSet
    cs = CandleSet("BTC", "5m", df, "binance", "USDT", NOW)
    assert cs.last_close_time == datetime(2026, 10, 9, 8, 5, tzinfo=UTC)
    assert cs.lag_seconds(NOW) == 150
    assert cs.is_fresh(NOW, 2.5)
    clock.advance(minutes=30)
    assert not cs.is_fresh(clock.now(), 2.5)


@respx.mock
def test_spot_price_binance():
    respx.get("https://data-api.binance.vision/api/v3/ticker/price").mock(
        return_value=httpx.Response(200, json={"symbol": "BTCUSDT", "price": "82692.65"}))
    assert BinanceProvider(httpx.Client(), FakeClock(NOW)).spot_price(BTC) == 82692.65


@pytest.mark.network
def test_live_binance_candles():
    from desk.clock import Clock
    clock = Clock()
    cs = CandleFeed([BinanceProvider(httpx.Client(timeout=15), clock)], clock).get(BTC, "5m", 50)
    assert len(cs.df) == 50 and cs.is_fresh(clock.now(), 2.5)
