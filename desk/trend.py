"""Daily trend-following (strategy.mode: trend).

A classic, well-studied rule (a "Donchian breakout" with a trend filter):
  BUY  when a day closes above the highest high of the previous N days
       (default 20) AND above its 50-day average: a strong, established uptrend.
  EXIT with a trailing stop K daily ATRs (default 3) below the highest price
       since buying: it gives the trend room to breathe and only sells once it
       has clearly turned. No fixed take-profit; winners are allowed to run.
It trades rarely (a few times a month across all coins), so fees and spread
are a small share of each trade, and it sits in cash during downtrends.
"""

from __future__ import annotations

import pandas as pd

from desk import indicators as ind
from desk.config import TrendConfig
from desk.strategy import Signal

TREND_COLUMNS = ["close", "prior_high", "ema", "atr_pct"]
NO_TARGET_PCT = 1000.0      # "no fixed take-profit": a target 10x away that is never reached in practice


def trend_frame(daily: pd.DataFrame, t: TrendConfig) -> pd.DataFrame:
    """One row per day. prior_high excludes the current day, so there is no look-ahead."""
    out = pd.DataFrame(index=daily.index)
    out["close"] = daily["close"]
    out["prior_high"] = ind.highest(daily["high"], t.breakout_days)
    out["ema"] = ind.ema(daily["close"], t.trend_ema_days)
    out["atr_pct"] = ind.atr(daily, t.atr_days) / daily["close"] * 100
    return out


def trend_signal(coin: str, values, t: TrendConfig) -> Signal | None:
    """values: one trend_frame row (Series or sequence in TREND_COLUMNS order)."""
    v = dict(zip(TREND_COLUMNS, [values[c] for c in TREND_COLUMNS] if isinstance(values, pd.Series) else values))
    if any(pd.isna(v[c]) for c in TREND_COLUMNS):
        return None
    if not (v["close"] > v["prior_high"] and v["close"] > v["ema"]):
        return None
    dist = t.stop_atr_multiple * v["atr_pct"]
    return Signal(
        coin=coin, kind="trend_breakout", stop_pct=dist, target_pct=NO_TARGET_PCT,
        reasons=(f"daily close above the {t.breakout_days}-day high",
                 f"above its {t.trend_ema_days}-day average (uptrend)",
                 f"stop and trailing stop {t.stop_atr_multiple:g} ATR = {dist:.1f}% below"),
        trail_pct=dist, max_hold_hours=t.max_hold_days * 24,
    )
