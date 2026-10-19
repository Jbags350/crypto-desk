"""1-minute prices, split into trading days (New York time)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from desk.futures.specs import Contract

NY = "America/New_York"


@dataclass
class Day:
    date: pd.Timestamp
    minutes: np.ndarray        # minutes since midnight NY for each bar in the session
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    prev_close: float          # previous session's close
    atr: float                 # 14-day average daily range (prior days only)
    trend_up: bool | None      # previous close above its 20-day average (None = not enough history)


def load_minutes(folder: Path, code: str) -> pd.DataFrame:
    parts = []
    for f in sorted(Path(folder).glob(f"{code}_*.csv.gz")):
        parts.append(pd.read_csv(f))
    df = pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    idx = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(NY)
    return pd.DataFrame({"o": df["open"].to_numpy(), "h": df["high"].to_numpy(), "l": df["low"].to_numpy(),
                         "c": df["close"].to_numpy()}, index=pd.DatetimeIndex(idx))


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def sessions(df: pd.DataFrame, contract: Contract) -> list[Day]:
    """One Day per weekday with a full-enough main session (bars inside the session only)."""
    start, end = (_hm(x) for x in contract.session)
    mins = (df.index.hour * 60 + df.index.minute).to_numpy()
    inside = (mins >= start) & (mins < end)
    sub = df[inside]
    sub_mins = mins[inside]
    dates = sub.index.normalize()
    days: list[Day] = []
    closes, ranges = [], []
    for d, idx in pd.Series(np.arange(len(sub)), index=dates).groupby(level=0):
        rows = idx.to_numpy()
        if len(rows) < (end - start) * 0.8 or d.weekday() >= 5:
            continue
        o, h, l, c = (sub[k].to_numpy()[rows] for k in ("o", "h", "l", "c"))
        prev_close = closes[-1] if closes else np.nan
        atr = float(np.mean(ranges[-14:])) if len(ranges) >= 14 else np.nan
        ma20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else np.nan
        trend = None if not np.isfinite(ma20) else bool(prev_close > ma20)
        days.append(Day(d, sub_mins[rows], o, h, l, c, prev_close, atr, trend))
        closes.append(float(c[-1]))
        ranges.append(float(h.max() - l.min()))
    return days
