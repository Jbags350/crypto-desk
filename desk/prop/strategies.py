"""Trading strategies for prop accounts: long AND short, all coins at once.

Each strategy looks at the market (desk.prop.market.Market) and returns
Signals: for every hourly candle close and every coin, whether to open a long
(+1) or a short (-1), how far away the stop-loss goes, how the trade is
managed, and when to close it early. The simulator (sim.py) does the rest.

  trend      the tested daily trend rule, both directions: 20-day breakout
             beyond the 50-day average, wide trailing stop. Holds days-weeks.
  breakout   intraday: price breaks the last 24-72 hours' range in the
             direction of the daily trend, on above-average volume.
  pullback   intraday: in an uptrend buy a sharp short-term dip (2-hour RSI
             very low); in a downtrend sell a sharp bounce. Quick exits.
  rotation   every day: rank the coins by recent strength. Buy the strongest
             in a rising market, short the weakest in a falling one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from desk.prop.market import Market


@dataclass
class Signals:
    name: str
    entry: np.ndarray            # (T, N) int8: +1 open long, -1 open short, 0 nothing
    stop_dist: np.ndarray        # (T, N) price distance from entry to the stop-loss
    target_dist: np.ndarray      # (T, N) price distance to take-profit (nan = none)
    trail_dist: np.ndarray       # (T, N) trailing-stop distance from the best close (nan = none)
    max_hold: int                # hours
    exit_long: np.ndarray        # (T, N) bool: close any long at this candle's close
    exit_short: np.ndarray       # (T, N) bool
    score: np.ndarray            # (T, N) priority when more signals than free slots


def _blank(m: Market, name: str, max_hold: int) -> Signals:
    shape = (m.T, m.N)
    nan = np.full(shape, np.nan)
    return Signals(name, np.zeros(shape, np.int8), nan.copy(), nan.copy(), nan.copy(), max_hold,
                   np.zeros(shape, bool), np.zeros(shape, bool), np.zeros(shape))


def _finite(*arrays) -> np.ndarray:
    ok = np.ones(arrays[0].shape, bool)
    for a in arrays:
        ok &= np.isfinite(a)
    return ok


def trend(m: Market, *, stop_atr: float = 2.0, max_hold_days: int = 120, shorts: bool = True,
          breakout_days: int = 20, ema_days: int = 50) -> Signals:
    s = _blank(m, f"trend{breakout_days}", max_hold_days * 24)
    F = m.feats
    at_daily_close = (m.close_hour() == 0)[:, None] & m.valid
    c, hh, ll = F["close_d"], F[f"hh{breakout_days}_d"], F[f"ll{breakout_days}_d"]
    e50, a = F[f"ema{ema_days}_d"], F["atr20_d"]
    ok = at_daily_close & _finite(c, hh, ll, e50, a)
    long_ = ok & (c > hh) & (c > e50)
    short = ok & (c < ll) & (c < e50) & shorts
    s.entry[long_] = 1
    s.entry[short] = -1
    dist = stop_atr * a
    s.stop_dist[:] = dist
    s.trail_dist[:] = dist
    s.score[:] = np.where(long_, (c - hh) / a, np.where(short, (ll - c) / a, 0.0))
    return s


def breakout(m: Market, *, lookback: int = 48, stop_atr: float = 3.0, trail_atr: float = 3.0,
             max_hold: int = 48, min_vol: float = 1.5, shorts: bool = True) -> Signals:
    s = _blank(m, f"breakout{lookback}", max_hold)
    F = m.feats
    c, a = m.c, F["atr_h"]
    hh, ll, e50d, vr = F[f"hh{lookback}"], F[f"ll{lookback}"], F["ema50_d"], F["vol_ratio"]
    ok = m.valid & _finite(hh, ll, e50d, a, vr) & (vr >= min_vol)
    long_ = ok & (c > hh) & (c > e50d)
    short = ok & (c < ll) & (c < e50d) & shorts
    s.entry[long_] = 1
    s.entry[short] = -1
    s.stop_dist[:] = stop_atr * a
    s.trail_dist[:] = trail_atr * a
    s.score[:] = vr
    return s


def pullback(m: Market, *, rsi_in: float = 10.0, rsi_out: float = 70.0, stop_atr: float = 2.0,
             max_hold: int = 24, shorts: bool = True) -> Signals:
    s = _blank(m, "pullback", max_hold)
    F = m.feats
    c, a, r2 = m.c, F["atr_h"], F["rsi2_h"]
    e50d, e50h, e200h = F["ema50_d"], F["ema50_h"], F["ema200_h"]
    ok = m.valid & _finite(a, r2, e50d, e50h, e200h)
    up = ok & (c > e50d) & (e50h > e200h)
    down = ok & (c < e50d) & (e50h < e200h)
    long_ = up & (r2 < rsi_in)
    short = down & (r2 > 100 - rsi_in) & shorts
    s.entry[long_] = 1
    s.entry[short] = -1
    s.stop_dist[:] = stop_atr * a
    s.exit_long[:] = ok & (r2 > rsi_out)
    s.exit_short[:] = ok & (r2 < 100 - rsi_out)
    s.score[:] = np.where(long_, rsi_in - r2, np.where(short, r2 - (100 - rsi_in), 0.0))
    return s


def rotation(m: Market, *, lookback: int = 72, top: int = 2, stop_atr_d: float = 1.5,
             rebalance_hour: int = 0, shorts: bool = True, regime_coin: str | None = "BTC") -> Signals:
    """Daily at rebalance_hour (UTC, candle close): long the `top` strongest coins
    (if the regime coin is above its 50-day average), short the `top` weakest (if below).
    regime_coin=None (e.g. currencies): longs and shorts are both always allowed.
    Anything that drops out of the top list is closed at the next rebalance."""
    s = _blank(m, f"rotation{lookback}", 24 * 30)
    F = m.feats
    mom = F[f"ret{lookback}"] / (F["atr20_d"] / m.c)           # return in units of daily volatility
    at_reb = (m.close_hour() == rebalance_hour)
    if regime_coin is None:
        bull = bear = np.ones(m.T, bool)
    else:
        j = m.coins.index(regime_coin)
        bull = m.c[:, j] > F["ema50_d"][:, j]
        bear = m.c[:, j] < F["ema50_d"][:, j]
    above = m.c > F["ema50_d"]
    for t in np.flatnonzero(at_reb):
        row = mom[t]
        good = np.isfinite(row) & m.valid[t] & np.isfinite(F["atr20_d"][t])
        if good.sum() < top:
            continue
        order = np.argsort(np.where(good, row, np.nan))       # nan last
        ranked = [k for k in order if good[k]]
        best = ranked[-top:]
        worst = [k for k in ranked[:top] if k not in best]
        keep_long = np.zeros(m.N, bool)
        keep_short = np.zeros(m.N, bool)
        if bull[t]:
            for k in best:
                if row[k] > 0 and above[t, k]:
                    s.entry[t, k] = 1
                    keep_long[k] = True
        if bear[t] and shorts:
            for k in worst:
                if row[k] < 0 and not above[t, k]:
                    s.entry[t, k] = -1
                    keep_short[k] = True
        s.exit_long[t] = ~keep_long
        s.exit_short[t] = ~keep_short
    s.stop_dist[:] = stop_atr_d * F["atr20_d"]
    s.trail_dist[:] = stop_atr_d * F["atr20_d"]
    s.score[:] = np.abs(np.nan_to_num(mom))
    return s


STRATEGIES = {"trend": trend, "breakout": breakout, "pullback": pullback, "rotation": rotation}
