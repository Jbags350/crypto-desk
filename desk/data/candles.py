"""Price candles (open/high/low/close/volume) for the indicators.

CoinSpot has no candle API, so candles come from public exchange data:
  1. Binance (global volume, USDT prices)  - default
  2. Kraken  (AUD prices, thinner volume)  - fallback
The scanner turns these into RSI/MACD/EMA/ATR. Actual buy/sell prices
always come from CoinSpot itself.

Every DataFrame returned here:
  index   = candle OPEN time (UTC), oldest first
  columns = open, high, low, close, volume (floats)
  only fully closed candles (the still-forming one is dropped)
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd

from desk.clock import Clock
from desk.config import TIMEFRAME_SECONDS, CoinConfig

log = logging.getLogger("desk.candles")

COLUMNS = ["open", "high", "low", "close", "volume"]


class CandleError(Exception):
    """Couldn't get usable candles."""


@dataclass(frozen=True)
class CandleSet:
    coin: str
    timeframe: str
    df: pd.DataFrame
    provider: str
    quote_currency: str      # "USDT" (Binance) or "AUD" (Kraken)
    fetched_at: datetime

    @property
    def last_close(self) -> float:
        return float(self.df["close"].iloc[-1])

    @property
    def last_close_time(self) -> datetime:
        """When the newest candle closed."""
        return self.df.index[-1].to_pydatetime() + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])

    def lag_seconds(self, now: datetime) -> float:
        return (now - self.last_close_time).total_seconds()

    def is_fresh(self, now: datetime, max_lag_multiple: float) -> bool:
        return self.lag_seconds(now) <= TIMEFRAME_SECONDS[self.timeframe] * max_lag_multiple


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz=UTC), dtype=float)


def validate(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Check candles are sane; raise CandleError if not."""
    if df.empty:
        raise CandleError("no candles returned")
    if list(df.columns) != COLUMNS:
        raise CandleError(f"unexpected columns {list(df.columns)}")
    if df[COLUMNS].isna().any().any():
        raise CandleError("candles contain missing values")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise CandleError("candle times are out of order or duplicated")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise CandleError("candles contain zero or negative prices")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1) - 1e-9).any() or \
            (df["low"] > df[["open", "close", "high"]].min(axis=1) + 1e-9).any():
        raise CandleError("candle high/low don't contain open/close")
    if (df["volume"] < 0).any():
        raise CandleError("negative volume")
    return df


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class BinanceProvider:
    """Binance public klines. No API key. data-api.binance.vision is Binance's
    public market-data host; api.binance.com is the backup host."""

    name = "binance"
    quote_currency = "USDT"
    HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
    MAX_LIMIT = 1000

    def __init__(self, http: httpx.Client, clock: Clock):
        self._http = http
        self._clock = clock

    def fetch(self, coin: CoinConfig, timeframe: str, limit: int,
              start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
        params: dict = {"symbol": coin.binance, "interval": timeframe, "limit": min(limit + 1, self.MAX_LIMIT)}
        if start:
            params["startTime"] = int(start.timestamp() * 1000)
        if end:
            params["endTime"] = int(end.timestamp() * 1000)
        rows = self._get_json("/api/v3/klines", params)
        return self.parse(rows, self._clock.now()).tail(limit)

    def spot_price(self, coin: CoinConfig) -> float:
        data = self._get_json("/api/v3/ticker/price", {"symbol": coin.binance})
        try:
            price = float(data["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CandleError(f"unexpected Binance ticker reply: {str(data)[:200]}") from exc
        if price <= 0:
            raise CandleError("Binance returned a zero price")
        return price

    def _get_json(self, path: str, params: dict):
        errors = []
        for host in self.HOSTS:
            try:
                r = self._http.get(host + path, params=params)
                if r.status_code == 200:
                    return r.json()
                errors.append(f"{host} HTTP {r.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{host} {type(exc).__name__}")
        raise CandleError("Binance unavailable: " + "; ".join(errors))

    @staticmethod
    def parse(rows, now: datetime) -> pd.DataFrame:
        """Binance rows: [openTime, open, high, low, close, volume, closeTime, ...]."""
        if not isinstance(rows, list):
            raise CandleError(f"unexpected Binance reply: {str(rows)[:200]}")
        now_ms = now.timestamp() * 1000
        recs, idx = [], []
        for r in rows:
            if int(r[6]) >= now_ms:          # closeTime in the future = still forming
                continue
            idx.append(pd.Timestamp(int(r[0]), unit="ms", tz=UTC))
            recs.append([float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
        if not recs:
            return _empty()
        return pd.DataFrame(recs, columns=COLUMNS, index=pd.DatetimeIndex(idx, name="open_time"))


class KrakenProvider:
    """Kraken public OHLC in AUD. Only the latest 720 candles are available."""

    name = "kraken"
    quote_currency = "AUD"
    URL = "https://api.kraken.com/0/public/OHLC"
    INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

    def __init__(self, http: httpx.Client, clock: Clock):
        self._http = http
        self._clock = clock

    def fetch(self, coin: CoinConfig, timeframe: str, limit: int,
              start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
        if not coin.kraken:
            raise CandleError(f"no Kraken pair configured for {coin.code}")
        if timeframe not in self.INTERVALS:
            raise CandleError(f"Kraken doesn't offer {timeframe} candles")
        params: dict = {"pair": coin.kraken, "interval": self.INTERVALS[timeframe]}
        if start:
            params["since"] = int(start.timestamp())
        try:
            r = self._http.get(self.URL, params=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CandleError(f"Kraken unavailable: {type(exc).__name__}") from exc
        if r.status_code != 200 or data.get("error"):
            raise CandleError(f"Kraken error: HTTP {r.status_code} {data.get('error')}")
        df = self.parse(data.get("result") or {}, timeframe, self._clock.now())
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df.tail(limit)

    def spot_price(self, coin: CoinConfig) -> float:
        if not coin.kraken:
            raise CandleError(f"no Kraken pair configured for {coin.code}")
        try:
            r = self._http.get("https://api.kraken.com/0/public/Ticker", params={"pair": coin.kraken})
            data = r.json()
            ticker = next(v for v in (data.get("result") or {}).values() if isinstance(v, dict))
            price = float(ticker["c"][0])   # "c" = last trade [price, volume]
        except (httpx.HTTPError, ValueError, StopIteration, KeyError, TypeError, IndexError) as exc:
            raise CandleError(f"Kraken ticker unavailable: {type(exc).__name__}") from exc
        if price <= 0:
            raise CandleError("Kraken returned a zero price")
        return price

    @staticmethod
    def parse(result: dict, timeframe: str, now: datetime) -> pd.DataFrame:
        """Kraken rows: [time, open, high, low, close, vwap, volume, count]."""
        rows = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), None)
        if rows is None:
            raise CandleError("unexpected Kraken reply")
        step = TIMEFRAME_SECONDS[timeframe]
        recs, idx = [], []
        for r in rows:
            open_ts = int(r[0])
            if open_ts + step > now.timestamp():   # still forming
                continue
            idx.append(pd.Timestamp(open_ts, unit="s", tz=UTC))
            recs.append([float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])])
        if not recs:
            return _empty()
        return pd.DataFrame(recs, columns=COLUMNS, index=pd.DatetimeIndex(idx, name="open_time"))


# ---------------------------------------------------------------------------
# Feed: tries providers in order, validates, caches briefly
# ---------------------------------------------------------------------------

class CandleFeed:
    def __init__(self, providers: Sequence, clock: Clock, cache_seconds: float = 30,
                 monotonic: Callable[[], float] = time.monotonic):
        if not providers:
            raise ValueError("CandleFeed needs at least one provider")
        self.providers = list(providers)
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._mono = monotonic
        self._cache: dict[tuple[str, str, int], tuple[float, CandleSet]] = {}
        self._lock = threading.Lock()

    def get(self, coin: CoinConfig, timeframe: str, limit: int) -> CandleSet:
        key = (coin.code, timeframe, limit)
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._mono() - hit[0] < self._cache_seconds:
                return hit[1]

        errors = []
        for p in self.providers:
            try:
                df = validate(p.fetch(coin, timeframe, limit), timeframe)
            except CandleError as exc:
                errors.append(f"{p.name}: {exc}")
                log.warning("Candles for %s %s from %s failed: %s", coin.code, timeframe, p.name, exc)
                continue
            cs = CandleSet(coin=coin.code, timeframe=timeframe, df=df, provider=p.name,
                           quote_currency=p.quote_currency, fetched_at=self._clock.now())
            with self._lock:
                self._cache[key] = (self._mono(), cs)
            return cs
        raise CandleError(f"No candles for {coin.code} {timeframe}: " + " | ".join(errors))

    def spot_price(self, coin: CoinConfig) -> tuple[float, str, str]:
        """Current global price: (price, quote currency, provider name)."""
        errors = []
        for p in self.providers:
            try:
                return p.spot_price(coin), p.quote_currency, p.name
            except CandleError as exc:
                errors.append(f"{p.name}: {exc}")
        raise CandleError(f"No global price for {coin.code}: " + " | ".join(errors))


def build_providers(names: Sequence[str], http: httpx.Client, clock: Clock) -> list:
    table = {"binance": BinanceProvider, "kraken": KrakenProvider}
    return [table[n](http, clock) for n in names]
