"""Technical indicators, written out by hand so they are easy to check.

All functions take pandas Series/DataFrames (oldest row first) and return a
Series aligned to the input. Values are NaN until there is enough history.

RSI and ATR use J. Welles Wilder's original smoothing (seeded with a simple
average of the first `period` values), matching TradingView and StockCharts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average with the standard 2/(span+1) weighting.

    Seeded with the simple average of the first `span` values (as TradingView
    and StockCharts do), so long EMAs like EMA200 match the charts. Leading
    NaNs (e.g. the start of a MACD line) are skipped."""
    arr = series.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    valid = np.flatnonzero(~np.isnan(arr))
    if len(valid) < span:
        return pd.Series(out, index=series.index)
    start = valid[0]
    seed_end = start + span
    if np.isnan(arr[start:seed_end]).any():
        return pd.Series(out, index=series.index)
    alpha = 2.0 / (span + 1)
    prev = arr[start:seed_end].mean()
    out[seed_end - 1] = prev
    for i in range(seed_end, len(arr)):
        prev = alpha * arr[i] + (1 - alpha) * prev
        out[i] = prev
    return pd.Series(out, index=series.index)


def wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average: seed = mean of first `period` values,
    then avg = (prev_avg * (period - 1) + value) / period."""
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    valid = np.flatnonzero(~np.isnan(arr))
    if len(valid) < period:
        return pd.Series(out, index=values.index)
    start = valid[0]
    seed_end = start + period
    if np.isnan(arr[start:seed_end]).any():
        return pd.Series(out, index=values.index)
    avg = arr[start:seed_end].mean()
    out[seed_end - 1] = avg
    for i in range(seed_end, len(arr)):
        avg = (avg * (period - 1) + arr[i]) / period
        out[i] = avg
    return pd.Series(out, index=values.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, 0-100. Above 70 = overbought, below 30 = oversold."""
    delta = close.diff()
    gains = wilder_smooth(delta.clip(lower=0), period)
    losses = wilder_smooth((-delta).clip(lower=0), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gains / losses
        out = 100 - 100 / (1 + rs)
    out = out.where(losses != 0, 100.0)                       # only gains -> 100
    out = out.where(~((gains == 0) & (losses == 0)), 50.0)    # no movement at all -> 50
    return out.where(gains.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line and histogram (line - signal)."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1)
    tr = ranges.max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range: the typical size of one candle's move, in price units."""
    return wilder_smooth(true_range(df), period)


def volume_ratio(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """This candle's volume divided by the average of the previous `lookback` candles.
    2.0 means twice the normal volume (a volume spike)."""
    avg_prev = volume.shift(1).rolling(lookback, min_periods=lookback).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = volume / avg_prev
    return out.where(avg_prev > 0)


def highest(series: pd.Series, lookback: int, exclude_current: bool = True) -> pd.Series:
    """Highest value of the previous `lookback` rows (not counting this one by default)."""
    s = series.shift(1) if exclude_current else series
    return s.rolling(lookback, min_periods=lookback).max()
