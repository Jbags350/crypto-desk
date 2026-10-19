"""Downloads and caches historical candles from Binance.

Saved as CSV files in backtest.candle_cache (default data/candles/), one per
coin and candle size (5m for day/swing tests, 1h for long trend tests). Later
runs only download what's missing, so only the first run is slow.

Bigger candles (15m, 1h, 4h, 1d) are built from the base candles, so every
timeframe comes from the same data and lines up exactly.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from desk.config import TIMEFRAME_SECONDS, CoinConfig
from desk.data.candles import COLUMNS, BinanceProvider, CandleError
from desk.data.candles import _empty as empty_frame

log = logging.getLogger("desk.backtest.history")

BASE_TF = "5m"
BASE_STEP = timedelta(seconds=TIMEFRAME_SECONDS[BASE_TF])


def _cache_file(cache_dir: Path, coin: CoinConfig, tf: str) -> Path:
    return cache_dir / f"{coin.binance}_{tf}.csv"


def load_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty_frame()
    df = pd.read_csv(path, index_col="open_time", parse_dates=["open_time"])
    df.index = pd.DatetimeIndex(df.index, tz="UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    df.index = df.index.as_unit("ns")
    df.index.name = "open_time"
    return df[COLUMNS].astype(float)


def save_cache(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index = out.index.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.index.name = "open_time"
    out.to_csv(path)


def download(http: httpx.Client, coin: CoinConfig, start: datetime, end: datetime, now: datetime,
             tf: str = BASE_TF, pause: float = 0.15, progress=None) -> pd.DataFrame:
    """Page through Binance 1000 candles at a time from start to end."""
    step = timedelta(seconds=TIMEFRAME_SECONDS[tf])
    host_errors = []
    frames = []
    cursor = start
    while cursor < end:
        params = {"symbol": coin.binance, "interval": tf, "startTime": int(cursor.timestamp() * 1000),
                  "endTime": int(end.timestamp() * 1000) - 1, "limit": 1000}
        rows = None
        for host in BinanceProvider.HOSTS:
            try:
                r = http.get(host + "/api/v3/klines", params=params, timeout=30)
                if r.status_code == 200:
                    rows = r.json()
                    break
                host_errors.append(f"{host} HTTP {r.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                host_errors.append(f"{host} {type(exc).__name__}")
        if rows is None:
            raise CandleError(f"couldn't download {coin.binance} history: {'; '.join(host_errors[-2:])}")
        part = BinanceProvider.parse(rows, now)
        if part.empty:
            if rows:                       # only the still-forming candle came back
                break
            cursor += step * 1000          # a gap (e.g. before the coin was listed): jump ahead
            continue
        frames.append(part)
        cursor = part.index[-1].to_pydatetime() + step
        if progress:
            progress(coin.code, cursor, start, end)
        if len(rows) < 1000 and cursor >= end - step:
            break
        time.sleep(pause)
    if not frames:
        return empty_frame()
    return pd.concat(frames)


def get_history(http: httpx.Client, coin: CoinConfig, start: datetime, end: datetime, cache_dir: Path,
                now: datetime, offline: bool = False, progress=None, tf: str = BASE_TF) -> pd.DataFrame:
    """Candles from start to end, using the cache and downloading only gaps at either end."""
    path = _cache_file(cache_dir, coin, tf)
    step = timedelta(seconds=TIMEFRAME_SECONDS[tf])
    cached = load_cache(path)
    pieces = [cached]
    if not offline:
        if cached.empty:
            pieces.append(download(http, coin, start, end, now, tf, progress=progress))
        else:
            first, last = cached.index[0].to_pydatetime(), cached.index[-1].to_pydatetime()
            if start < first - step:
                pieces.append(download(http, coin, start, first, now, tf, progress=progress))
            if last + step < end:
                pieces.append(download(http, coin, last + step, end, now, tf, progress=progress))
    nonempty = [p for p in pieces if not p.empty]
    merged = pd.concat(nonempty) if nonempty else cached
    merged.index = pd.DatetimeIndex(merged.index).as_unit("ns")
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if len(merged) > len(cached):
        save_cache(merged, path)
    return merged[(merged.index >= pd.Timestamp(start)) & (merged.index < pd.Timestamp(end))]


def resample(base: pd.DataFrame, timeframe: str, base_tf: str | None = None) -> pd.DataFrame:
    """Build bigger candles from smaller ones. Incomplete buckets (gaps) are dropped."""
    if base_tf is None:
        base_tf = _infer_tf(base)
    if timeframe == base_tf:
        return base
    # fixed-length rules (24h, not "1D") so origin="epoch" really aligns buckets to UTC midnight
    rule = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "24h"}[timeframe]
    need = TIMEFRAME_SECONDS[timeframe] // TIMEFRAME_SECONDS[base_tf]
    g = base.resample(rule, label="left", closed="left", origin="epoch")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = g["close"].count()
    return out[counts == need].astype(float)


def _infer_tf(df: pd.DataFrame) -> str:
    if len(df) < 2:
        return BASE_TF
    seconds = int(pd.Series(df.index).diff().dropna().dt.total_seconds().median())
    for tf, s in TIMEFRAME_SECONDS.items():
        if s == seconds:
            return tf
    return BASE_TF
