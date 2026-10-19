"""Hourly prices for several coins on one shared clock, plus indicators.

Every row is one hourly candle (index = its OPEN time). A decision made "at
row t" uses only that candle's close and older data. Daily values are taken
from the last day that had fully closed by then, so nothing peeks ahead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from desk.backtest.history import get_history, resample
from desk.config import CoinConfig
from desk.indicators import atr, ema, rsi

HOUR = pd.Timedelta(hours=1)
DAY = pd.Timedelta(days=1)

# Tradeify 247's top-10 (Oct 2026) that have long Binance histories. HYPE was only
# listed on Binance in Sep 2026, so it can't be tested over years.
PROP_COINS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "XRP": "XRPUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT",
              "TRX": "TRXUSDT", "DOGE": "DOGEUSDT", "LINK": "LINKUSDT", "ZEC": "ZECUSDT"}


@dataclass
class Market:
    index: pd.DatetimeIndex           # hourly candle open times (UTC)
    coins: list[str]
    o: np.ndarray                     # shape (T, N)
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    valid: np.ndarray                 # (T, N) bool: a real candle (not a filled gap)
    feats: dict[str, np.ndarray] = field(default_factory=dict)   # name -> (T, N)

    @property
    def T(self) -> int:
        return len(self.index)

    @property
    def N(self) -> int:
        return len(self.coins)

    def close_hour(self) -> np.ndarray:
        """UTC hour at which each candle CLOSES (0-23)."""
        return ((self.index + HOUR).hour).to_numpy()

    def slice_rows(self, start: datetime, end: datetime) -> tuple[int, int]:
        a = int(self.index.searchsorted(pd.Timestamp(start)))
        b = int(self.index.searchsorted(pd.Timestamp(end)))
        return a, b


def load_hourly(coins: dict[str, str], start: datetime, end: datetime, cache_dir: Path,
                offline: bool = False) -> dict[str, pd.DataFrame]:
    out = {}
    with httpx.Client(timeout=30) as http:
        for code, symbol in coins.items():
            cc = CoinConfig(code=code, binance=symbol)
            out[code] = get_history(http, cc, start, end, cache_dir, datetime.now(start.tzinfo), offline=offline,
                                    tf="1h")
    return out


def _daily_on_hours(daily: pd.Series, hourly_index: pd.DatetimeIndex) -> np.ndarray:
    """Map a daily series onto hourly rows: row t sees the last day whose CLOSE <= row t's close."""
    s = pd.Series(daily.to_numpy(), index=daily.index + DAY)      # stamped at the day's close
    close_times = hourly_index + HOUR
    return s.reindex(s.index.union(close_times)).ffill().reindex(close_times).to_numpy()


def build_market(frames: dict[str, pd.DataFrame], start: datetime, end: datetime) -> Market:
    """Align coins on a full hourly grid and add the indicators the strategies use.
    `start`/`end` bound the grid; pass data from earlier than `start` for warm-up and
    it is used for the indicators but not kept in the grid."""
    coins = list(frames)
    first = min(df.index[0] for df in frames.values())
    grid_all = pd.date_range(first, pd.Timestamp(end) - HOUR, freq="1h", tz="UTC")
    keep = grid_all >= pd.Timestamp(start)
    cols = {k: [] for k in ("o", "h", "l", "c", "v", "valid")}
    feats: dict[str, list[np.ndarray]] = {}

    for code in coins:
        df = frames[code].reindex(grid_all)
        valid = df["close"].notna().to_numpy()
        close = df["close"].ffill()
        df = pd.DataFrame({"open": df["open"].fillna(close), "high": df["high"].fillna(close),
                           "low": df["low"].fillna(close), "close": close, "volume": df["volume"].fillna(0.0)})
        f = coin_features(df, frames[code])
        for name, arr in f.items():
            feats.setdefault(name, []).append(arr[keep])
        cols["o"].append(df["open"].to_numpy()[keep])
        cols["h"].append(df["high"].to_numpy()[keep])
        cols["l"].append(df["low"].to_numpy()[keep])
        cols["c"].append(df["close"].to_numpy()[keep])
        cols["v"].append(df["volume"].to_numpy()[keep])
        cols["valid"].append(valid[keep])

    stack = {k: np.column_stack(v) for k, v in cols.items()}
    return Market(grid_all[keep], coins, stack["o"], stack["h"], stack["l"], stack["c"], stack["v"],
                  stack["valid"].astype(bool), {k: np.column_stack(v) for k, v in feats.items()})


def coin_features(df: pd.DataFrame, raw: pd.DataFrame) -> dict[str, np.ndarray]:
    """Indicators for one coin on the hourly grid (all known at each candle's close)."""
    close = df["close"]
    f: dict[str, np.ndarray] = {}
    f["atr_h"] = atr(df, 14).to_numpy()
    f["rsi2_h"] = rsi(close, 2).to_numpy()
    f["rsi14_h"] = rsi(close, 14).to_numpy()
    f["ema50_h"] = ema(close, 50).to_numpy()
    f["ema200_h"] = ema(close, 200).to_numpy()
    for n in (24, 48, 72):
        f[f"hh{n}"] = df["high"].rolling(n).max().shift(1).to_numpy()   # highest high of the n hours BEFORE this one
        f[f"ll{n}"] = df["low"].rolling(n).min().shift(1).to_numpy()
    for n in (24, 72, 120, 168, 336, 720):
        f[f"ret{n}"] = (close / close.shift(n) - 1).to_numpy()
    vol_avg = df["volume"].rolling(48).mean().shift(1)
    f["vol_ratio"] = (df["volume"] / vol_avg.replace(0, np.nan)).to_numpy()

    # daily candles from the real (unfilled) data, UTC days
    daily = resample(raw, "1d", base_tf="1h")
    d_close = daily["close"]
    for n in (20, 50, 100):
        f[f"ema{n}_d"] = _daily_on_hours(ema(d_close, n), df.index)
    f["atr20_d"] = _daily_on_hours(atr(daily, 20), df.index)
    for n in (10, 20, 40):                     # highest high / lowest low of the prior n days, excl. this one
        f[f"hh{n}_d"] = _daily_on_hours(daily["high"].rolling(n).max().shift(1), df.index)
        f[f"ll{n}_d"] = _daily_on_hours(daily["low"].rolling(n).min().shift(1), df.index)
    f["close_d"] = _daily_on_hours(d_close, df.index)
    return f
