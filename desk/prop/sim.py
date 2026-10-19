"""Hour-by-hour simulator for a prop account (long and short, many coins).

How a candle (row t) is processed:
  1. stops / take-profits of open trades, using the candle's low/high
     (a stop that the price jumped past fills at the open; if a candle touches
     both the stop and the target, the stop is assumed to come first)
  2. the worst moment of the candle: every open trade at its worst price at
     once (pessimistic). The bot's daily brake and the firm's limits are
     checked against that
  3. at the close: trailing stops move, time / signal exits, the daily swap
     and day reset (22:00 UTC), payout or target checks, then new trades
Trades are sized so hitting the stop loses `risk_pct` of the account size,
limited by the firm's leverage (margin = position / leverage).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from desk.prop.market import Market
from desk.prop.rules import PropRules
from desk.prop.strategies import Signals


@dataclass(frozen=True)
class SimConfig:
    risk_pct: float = 0.5               # % of the account size lost if a trade hits its stop
    max_positions: int = 4
    max_same_side: int = 3              # crypto moves together: cap trades in one direction
    daily_brake_pct: float | None = 2.0  # bot closes everything and rests for the day at this loss
    brake_slip_pct: float = 0.25        # closing in a hurry costs this much more (% of account)
    margin_use: float = 0.9
    enforce_rules: bool = True          # end the run when a firm limit is broken
    target_pct: float | None = None     # close everything and stop once up this much (passing a phase)
    payout_every_days: int | None = None  # funded accounts: withdraw profit this often
    payout_keep_pct: float = 0.0        # leave this much profit in (a cushion; matters once the floor locks)
    max_days: float | None = None
    floor_scaling: bool = False         # trade smaller as the account nears the max-loss floor
    scale_up_max: float = 1.0           # >1: trade bigger once ahead (house money), up to this multiple


@dataclass
class Position:
    j: int
    side: int
    qty: float
    entry: float
    entry_t: int
    stop: float
    target: float
    trail: float
    best: float
    fees: float
    swap: float = 0.0
    trailing_moved: bool = False


@dataclass
class SimResult:
    outcome: str                        # pass | breach_daily | breach_max | end | timeout
    start_t: int
    end_t: int
    equity: np.ndarray                  # equity at each candle close
    trades: list[dict] = field(default_factory=list)
    payouts: float = 0.0                # trader's share of withdrawals (funded runs)
    worst_day_pct: float = 0.0
    brakes: int = 0
    payout_count: int = 0
    first_payout_t: int | None = None
    blocked_by_consistency: int = 0     # payout dates skipped because one day was too big a share
    payout_log: list[tuple[int, float]] = field(default_factory=list)   # (row, amount paid to the trader)

    @property
    def hours(self) -> int:
        return self.end_t - self.start_t + 1


def simulate(m: Market, sig: Signals, rules: PropRules, cfg: SimConfig, start_t: int, end_t: int | None = None
             ) -> SimResult:
    end_t = m.T if end_t is None else min(end_t, m.T)
    if cfg.max_days is not None:
        end_t = min(end_t, start_t + int(cfg.max_days * 24))
    acct = rules.account_size
    half = np.array([rules.half_cost_pct(c) / 100 for c in m.coins])
    stop_slip = rules.stop_slippage_pct / 100
    lev = np.array([rules.lev(c) for c in m.coins])
    fee = rules.fee_pct / 100
    swap_rate = rules.swap_pct_per_day / 100
    reset_rows = m.close_hour() == rules.daily_reset_hour_utc
    o, h, l, c = m.o, m.h, m.l, m.c

    balance = acct
    day_ref = acct
    max_loss_usd = acct - rules.floor_usd
    floor = rules.floor_usd             # trails up on trailing-floor accounts
    max_closed = acct
    floor_locked = False
    day_start_balance = acct
    day_realized: list[float] = []      # realized result of each finished day since the last payout
    payout_count = 0
    first_payout_t = None
    blocked = 0
    payout_log: list[tuple[int, float]] = []
    halted = False
    positions: dict[int, Position] = {}
    trades: list[dict] = []
    equity = np.empty(end_t - start_t)
    payouts = 0.0
    last_payout_t = start_t
    worst_day = 0.0
    brakes = 0
    outcome = "end"
    t = start_t

    def raise_floor() -> None:
        nonlocal floor, max_closed
        if rules.trailing_floor and not floor_locked and balance > max_closed:
            max_closed = balance
            floor = min(acct, max_closed - max_loss_usd)

    def close(p: Position, price: float, t_: int, reason: str) -> None:
        nonlocal balance
        f = abs(p.qty * price) * fee
        pnl = p.side * p.qty * (price - p.entry) - f
        balance += pnl
        raise_floor()
        positions.pop(p.j)
        trades.append({"coin": m.coins[p.j], "side": p.side, "open_t": p.entry_t, "close_t": t_,
                       "entry": p.entry, "exit": price, "qty": p.qty, "reason": reason,
                       "pnl": pnl - p.fees - p.swap, "fees": p.fees + f, "swap": p.swap})

    def unreal(p: Position, price: float) -> float:
        return p.side * p.qty * (price - p.entry)

    for t in range(start_t, end_t):
        # 1) stops and targets inside the candle
        for p in list(positions.values()):
            if p.entry_t >= t:
                continue
            j, s = p.j, p.side
            hit_stop = l[t, j] <= p.stop if s > 0 else h[t, j] >= p.stop
            if hit_stop:
                gap = o[t, j] <= p.stop if s > 0 else o[t, j] >= p.stop
                px = o[t, j] if gap else p.stop
                close(p, px * (1 - s * (half[j] + stop_slip)), t, "trail" if p.trailing_moved else "stop")
                continue
            if np.isfinite(p.target):
                hit_tp = h[t, j] >= p.target if s > 0 else l[t, j] <= p.target
                if hit_tp:
                    gap = o[t, j] >= p.target if s > 0 else o[t, j] <= p.target
                    px = o[t, j] if gap else p.target
                    close(p, px * (1 - s * half[j]), t, "target")

        # 2) worst moment of the candle: brake, then firm limits
        worst_u = sum(unreal(p, l[t, p.j] if p.side > 0 else h[t, p.j]) for p in positions.values())
        worst_eq = balance + worst_u
        if cfg.daily_brake_pct is not None and not halted:
            brake_level = day_ref - acct * cfg.daily_brake_pct / 100
            if worst_eq <= brake_level:
                # The live bot checks every minute, so it closes everything about when the
                # loss reaches the brake level (plus some slippage), never worse than the
                # candle's actual extreme.
                brakes += 1
                halted = True
                if positions:
                    exit_eq = brake_level - acct * cfg.brake_slip_pct / 100
                    share = 1.0
                    if worst_u < 0 and balance > exit_eq:
                        share = min(1.0, (exit_eq - balance) / worst_u)    # part of each worst-case loss taken
                    for p in list(positions.values()):
                        w = l[t, p.j] if p.side > 0 else h[t, p.j]
                        close(p, p.entry + (w - p.entry) * share, t, "brake")
                    worst_eq = balance
        day_loss = (day_ref - worst_eq) / acct * 100
        worst_day = max(worst_day, day_loss)
        if cfg.enforce_rules:
            if worst_eq < day_ref - rules.daily_loss_usd:
                outcome = "breach_daily"
                equity[t - start_t] = worst_eq
                break
            if worst_eq < floor:
                outcome = "breach_max"
                equity[t - start_t] = worst_eq
                break

        # 3) at the close
        for p in list(positions.values()):
            j, s = p.j, p.side
            if np.isfinite(p.trail):
                p.best = max(p.best, c[t, j]) if s > 0 else min(p.best, c[t, j])
                new_stop = p.best - s * p.trail
                if (s > 0 and new_stop > p.stop) or (s < 0 and new_stop < p.stop):
                    p.stop = new_stop
                    p.trailing_moved = True
            if t - p.entry_t >= sig.max_hold:
                close(p, c[t, j] * (1 - s * half[j]), t, "time")
            elif (s > 0 and sig.exit_long[t, j]) or (s < 0 and sig.exit_short[t, j]):
                close(p, c[t, j] * (1 - s * half[j]), t, "signal")

        if reset_rows[t]:
            for p in positions.values():
                sw = abs(p.qty * c[t, p.j]) * swap_rate
                balance -= sw
                p.swap += sw
            eq_now = balance + sum(unreal(p, c[t, p.j]) for p in positions.values())
            day_ref = balance if rules.daily_ref == "balance" else max(balance, eq_now)
            day_realized.append(balance - day_start_balance)
            day_start_balance = balance
            halted = False
            if cfg.payout_every_days and (t - last_payout_t) >= cfg.payout_every_days * 24:
                profit = balance - acct * (1 + cfg.payout_keep_pct / 100)
                ok = profit >= 100
                if ok and rules.consistency_pct is not None:
                    total = sum(day_realized)
                    best = max(day_realized, default=0.0)
                    ok = total > 0 and best / total * 100 <= rules.consistency_pct
                    blocked += 0 if ok else 1
                if ok:
                    payouts += profit * rules.profit_split
                    payout_log.append((t, profit * rules.profit_split))
                    balance -= profit
                    day_ref -= profit
                    day_start_balance = balance
                    day_realized = []
                    payout_count += 1
                    first_payout_t = first_payout_t if first_payout_t is not None else t
                    if rules.payout_locks_floor:
                        floor, floor_locked = acct, True
                last_payout_t = t

        eq = balance + sum(unreal(p, c[t, p.j]) for p in positions.values())
        if cfg.target_pct is not None and eq >= acct * (1 + cfg.target_pct / 100):
            for p in list(positions.values()):
                close(p, c[t, p.j] * (1 - p.side * half[p.j]), t, "target_reached")
            eq = balance
            if balance >= acct * (1 + cfg.target_pct / 100):
                equity[t - start_t] = eq
                outcome = "pass"
                break

        if not halted:
            cand = np.flatnonzero(sig.entry[t] != 0)
            if len(cand):
                cand = cand[np.argsort(-sig.score[t, cand])]
                used = sum(abs(p.qty * c[t, p.j]) / lev[p.j] for p in positions.values())
                for j in cand:
                    if j in positions or len(positions) >= cfg.max_positions or not m.valid[t, j]:
                        continue
                    s = int(sig.entry[t, j])
                    if sum(1 for p in positions.values() if p.side == s) >= cfg.max_same_side:
                        continue
                    dist = sig.stop_dist[t, j]
                    if not np.isfinite(dist) or dist <= 0:
                        continue
                    px = c[t, j] * (1 + s * half[j])
                    risk = cfg.risk_pct
                    if cfg.floor_scaling:
                        # full size with the whole cushion left, half size with half of it, never under a quarter
                        cushion = (eq - floor) / max_loss_usd
                        risk *= min(cfg.scale_up_max, max(0.25, cushion))
                    qty = acct * risk / 100 / dist
                    room = max(0.0, eq * cfg.margin_use - used) * lev[j]
                    qty = min(qty, room / px)
                    if qty * px < 10:
                        continue
                    f = qty * px * fee
                    balance -= f
                    tgt = px + s * sig.target_dist[t, j] if np.isfinite(sig.target_dist[t, j]) else np.nan
                    trail = sig.trail_dist[t, j] if np.isfinite(sig.trail_dist[t, j]) else np.nan
                    positions[j] = Position(j, s, qty, px, t, px - s * dist, tgt, trail, c[t, j], f)
                    used += qty * px / lev[j]
                    eq -= f

        equity[t - start_t] = balance + sum(unreal(p, c[t, p.j]) for p in positions.values())
    else:
        t = end_t - 1
        outcome = "timeout" if cfg.max_days is not None and end_t - start_t >= cfg.max_days * 24 else "end"

    if outcome in ("end", "timeout"):
        for p in list(positions.values()):
            close(p, c[t, p.j] * (1 - p.side * half[p.j]), t, "end")
        equity[t - start_t] = balance
    return SimResult(outcome, start_t, t, equity[: t - start_t + 1], trades, payouts, worst_day, brakes,
                     payout_count, first_payout_t, blocked, payout_log)


def trades_frame(m: Market, res: SimResult) -> pd.DataFrame:
    df = pd.DataFrame(res.trades)
    if df.empty:
        return df
    df["opened"] = m.index[df["open_t"].to_numpy()] + pd.Timedelta(hours=1)
    df["closed"] = m.index[df["close_t"].to_numpy()] + pd.Timedelta(hours=1)
    df["hours"] = df["close_t"] - df["open_t"]
    return df
