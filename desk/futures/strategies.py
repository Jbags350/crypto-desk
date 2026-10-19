"""Day-trading strategies on 1-minute bars. Every trade is closed before the session ends.

Each strategy turns one Day into at most one Trade (entry, exit, side and the worst
adverse move while it was open, in price points). Fills are pessimistic:
  - breakout entries are stop orders: they fill one tick beyond the level
  - stops fill one tick worse than the stop price; a bar touching both the stop and
    the target counts as a stop
  - targets are limit orders (fill at the price); time exits use the bar's close
Known effects tested (see research for results):
  orb          opening-range breakout (first N minutes' high/low), with or without the
               daily trend as a filter; stop at the other side or the middle of the range
  momentum     the first half-hour's move predicts the last half-hour's (documented for
               US index futures): enter at 15:30, exit at the close
  gap_fade     a big opening gap tends to partly close: fade it, target the prior close
  trend_day    if price has moved far from the open by 10:30, follow it to the close
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from desk.futures.data import Day, _hm
from desk.futures.specs import Contract


@dataclass
class Trade:
    date: object
    side: int                  # +1 long, -1 short
    entry: float
    exit: float
    mae: float                 # worst adverse move in points (>= 0)
    slip_ticks: int            # ticks of slippage to charge (entries and exits that are market/stop)
    reason: str
    risk: float = 0.0          # planned risk: entry to stop, in points (for sizing)

    def dollars(self, ct: Contract, contracts: int = 1) -> float:
        gross = self.side * (self.exit - self.entry) * ct.point_value
        cost = ct.commission + self.slip_ticks * ct.tick * ct.point_value
        return (gross - cost) * contracts

    def worst_dollars(self, ct: Contract, contracts: int = 1) -> float:
        return (-self.mae * ct.point_value - ct.commission) * contracts


def _run_trade(d: Day, i0: int, side: int, entry: float, stop: float, target: float | None, last: int,
               ct: Contract, entry_slip: int) -> Trade:
    """Walk bars i0..last after entry. Exit at stop (1 tick worse), target, or the close of bar `last`."""
    worst = 0.0
    for i in range(i0, last + 1):
        lo, hi = d.l[i], d.h[i]
        adverse = (entry - lo) if side > 0 else (hi - entry)
        hit_stop = lo <= stop if side > 0 else hi >= stop
        hit_tgt = target is not None and (hi >= target if side > 0 else lo <= target)
        if hit_stop:
            px = (min(stop, d.o[i]) if side > 0 else max(stop, d.o[i])) - side * ct.tick
            worst = max(worst, side * (entry - px))
            return Trade(d.date, side, entry, px, worst, entry_slip, "stop", abs(entry - stop))
        worst = max(worst, adverse)
        if hit_tgt:
            return Trade(d.date, side, entry, target, worst, entry_slip, "target", abs(entry - stop))
    return Trade(d.date, side, entry, d.c[last], worst, entry_slip + 1, "time", abs(entry - stop))


def _bar_at(d: Day, hhmm: str) -> int | None:
    m = _hm(hhmm)
    idx = np.searchsorted(d.minutes, m)
    return int(idx) if idx < len(d.minutes) else None


def _last_bar(d: Day, ct: Contract, minutes_before_end: int = 5) -> int:
    end = _hm(ct.session[1]) - minutes_before_end
    return int(max(0, np.searchsorted(d.minutes, end) - 1))


def orb(d: Day, ct: Contract, *, minutes: int = 15, stop: str = "range", target_r: float | None = None,
        trend_filter: bool = False) -> Trade | None:
    start = d.minutes[0]
    k = int(np.searchsorted(d.minutes, start + minutes))
    if k < 3 or k >= len(d.minutes) - 10:
        return None
    hi, lo = d.h[:k].max(), d.l[:k].min()
    rng = hi - lo
    if rng <= 0:
        return None
    last = _last_bar(d, ct)
    for i in range(k, last):
        up, dn = d.h[i] > hi, d.l[i] < lo
        if up and dn:
            return None                                       # both sides in one bar: unclear, skip the day
        if up or dn:
            side = 1 if up else -1
            if trend_filter and d.trend_up is not None and (side > 0) != d.trend_up:
                return None
            entry = (max(hi, d.o[i]) + ct.tick) if side > 0 else (min(lo, d.o[i]) - ct.tick)
            stop_px = (lo if side > 0 else hi) if stop == "range" else (hi + lo) / 2
            risk = abs(entry - stop_px)
            tgt = entry + side * target_r * risk if target_r else None
            return _run_trade(d, i, side, entry, stop_px, tgt, last, ct, entry_slip=0)
    return None


def momentum(d: Day, ct: Contract, *, signal_until: str = "10:00", enter_at: str = "15:30",
             min_move_atr: float = 0.0) -> Trade | None:
    if not np.isfinite(d.prev_close) or not np.isfinite(d.atr):
        return None
    i_sig = _bar_at(d, signal_until)
    i_in = _bar_at(d, enter_at)
    if i_sig is None or i_in is None or i_sig == 0:
        return None
    move = d.c[i_sig - 1] - d.prev_close
    if abs(move) < min_move_atr * d.atr or move == 0:
        return None
    side = 1 if move > 0 else -1
    entry = d.o[i_in]
    last = _last_bar(d, ct, minutes_before_end=1)
    stop_px = entry - side * 0.5 * d.atr
    return _run_trade(d, i_in, side, entry, stop_px, None, last, ct, entry_slip=1)


def gap_fade(d: Day, ct: Contract, *, min_gap_atr: float = 0.3, stop_mult: float = 1.0) -> Trade | None:
    if not np.isfinite(d.prev_close) or not np.isfinite(d.atr):
        return None
    gap = d.o[0] - d.prev_close
    if abs(gap) < min_gap_atr * d.atr:
        return None
    side = -1 if gap > 0 else 1
    entry = d.c[0]
    stop_px = entry - side * stop_mult * abs(gap)
    return _run_trade(d, 1, side, entry, stop_px, d.prev_close, _last_bar(d, ct), ct, entry_slip=1)


def trend_day(d: Day, ct: Contract, *, at: str = "10:30", min_move_atr: float = 0.5) -> Trade | None:
    if not np.isfinite(d.atr):
        return None
    i = _bar_at(d, at)
    if i is None or i == 0:
        return None
    move = d.c[i - 1] - d.o[0]
    if abs(move) < min_move_atr * d.atr:
        return None
    side = 1 if move > 0 else -1
    entry = d.o[i]
    stop_px = d.o[0]                                          # back to the open = the idea failed
    return _run_trade(d, i, side, entry, stop_px, None, _last_bar(d, ct), ct, entry_slip=1)


STRATEGIES = {"orb": orb, "momentum": momentum, "gap_fade": gap_fade, "trend_day": trend_day}


@dataclass
class ScaledDay:
    """Result of one day of a scaled-position strategy, per 1 contract at 100% weight."""
    pnl_pts: float             # price points earned (before costs)
    worst_pts: float           # worst moment of the day in points (<= 0)
    turnover: float            # contracts traded (each 1.0 = one contract bought or sold)

    def dollars(self, ct: Contract, contracts: float = 1.0) -> float:
        per_side = ct.commission / 2 + 0.5 * ct.tick * ct.point_value      # TWAP fills: half a tick of slippage
        return (self.pnl_pts * ct.point_value - self.turnover * per_side) * contracts

    def worst_dollars(self, ct: Contract, contracts: float = 1.0) -> float:
        return self.worst_pts * ct.point_value * contracts


def twap_momentum(d: Day, ct: Contract, *, side: str = "short", start_pct: float = 0.5, full_pct: float = 1.5,
                  window: int = 30, band: float = 0.1, max_step: float = 0.5) -> ScaledDay | None:
    """Goldman Sachs Xenith's intraday momentum rule (GSISMEST), as published:
    every `window` minutes the time-weighted average price (TWAP) is compared with yesterday's
    close. Fallen more than start_pct % -> start going short, fully short at full_pct %
    (side="long" mirrors it for rises, "both" does both). During the next window the position moves
    toward that target at the window's TWAP (only if more than `band` away, at most `max_step` per
    window). Everything is closed at the end of the day. Goldman's exact thresholds are not public;
    these are parameters to test."""
    if not np.isfinite(d.prev_close) or len(d.c) < window * 4:
        return None
    n_win = len(d.c) // window
    twaps = np.array([d.c[k * window:(k + 1) * window].mean() for k in range(n_win)])
    pos = 0.0
    entry_px = 0.0                 # price the current position is marked from
    pnl = 0.0
    worst = 0.0
    turnover = 0.0
    for k in range(n_win):
        if k > 0:                                         # execute last window's target at this window's TWAP
            s = twaps[k - 1] / d.prev_close - 1
            tgt = 0.0
            frac = lambda x: min(1.0, max(0.0, (x * 100 - start_pct) / max(full_pct - start_pct, 1e-9)))
            if side in ("short", "both") and s < 0:
                tgt = -frac(-s)
            if side in ("long", "both") and s > 0:
                tgt = frac(s)
            if abs(tgt - pos) > band:
                step = max(-max_step, min(max_step, tgt - pos))
                px = twaps[k]
                pnl += pos * (px - entry_px)
                entry_px = px
                pos += step
                turnover += abs(step)
        seg = slice(k * window, (k + 1) * window)
        if pos != 0:
            adverse = (d.l[seg].min() - entry_px) if pos > 0 else (d.h[seg].max() - entry_px)
            worst = min(worst, pnl + pos * adverse)
    if pos != 0:
        pnl += pos * (d.c[-1] - entry_px)
        turnover += abs(pos)
    return ScaledDay(pnl, worst, turnover)
