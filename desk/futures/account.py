"""A Tradeify Select Flex account, day by day.

Loss limit: `drawdown` below the highest END-OF-DAY balance, but it never trails above
the starting balance + $100 (then it's fixed). It's checked live: if a day's worst moment
(every trade at its worst point) touches it, the account is lost.
Evaluation: reach +target with the best day no more than 40% of the total profit.
Funded: a payout can be asked for after every 5 winning days (each at least
`winning_day_min`), up to the cap and at most half the profit; 90% goes to the trader.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from desk.futures.specs import FuturesRules


@dataclass
class DayResult:
    pnl: float                 # the day's result in US$ (all trades, after costs)
    worst: float               # the day's worst moment in US$ (<= 0)


@dataclass
class AccountRun:
    outcome: str               # pass | breach | end | (funded) end
    days: int
    payouts: list[tuple[int, float]] = field(default_factory=list)   # (day index, paid to trader)
    end_balance: float = 0.0

    @property
    def paid(self) -> float:
        return sum(p for _, p in self.payouts)


def run_account(days: list[DayResult], rules: FuturesRules, start: int, *, mode: str = "eval",
                max_days: int | None = None) -> AccountRun:
    size = rules.size
    balance = size
    eod_high = size
    threshold = size - rules.drawdown
    best_day = 0.0
    winning_days = 0
    run = AccountRun("end", 0)
    stop = len(days) if max_days is None else min(len(days), start + max_days)
    i = start
    for i in range(start, stop):
        d = days[i]
        if balance + d.worst <= threshold:
            run.outcome, run.days, run.end_balance = "breach", i - start + 1, balance + d.worst
            return run
        balance += d.pnl
        eod_high = max(eod_high, balance)
        threshold = min(eod_high - rules.drawdown, rules.lock_level)
        best_day = max(best_day, d.pnl)
        if mode == "eval":
            profit = balance - size
            if profit >= rules.target and best_day <= rules.eval_consistency * profit:
                run.outcome, run.days, run.end_balance = "pass", i - start + 1, balance
                return run
        else:
            if d.pnl >= rules.winning_day_min:
                winning_days += 1
            profit = balance - size
            if winning_days >= rules.payout_winning_days and profit > 0:
                amount = min(rules.payout_cap, 0.5 * profit)
                if amount >= 250:
                    balance -= amount
                    run.payouts.append((i, amount * rules.profit_split))
                    winning_days = 0
    run.days = stop - start
    run.end_balance = balance
    return run


def per_day(trades_by_day: dict, n_days: list, contracts_fn, contract) -> list[DayResult]:
    """Day results for a strategy: trades_by_day maps a date to its Trade (or None)."""
    out = []
    for date in n_days:
        t = trades_by_day.get(date)
        if t is None:
            out.append(DayResult(0.0, 0.0))
            continue
        n = contracts_fn(t)
        out.append(DayResult(t.dollars(contract, n), min(0.0, t.worst_dollars(contract, n))))
    return out


def summarize(runs: list[AccountRun]) -> dict:
    n = max(1, len(runs))
    passed = [r for r in runs if r.outcome == "pass"]
    return {"n": len(runs), "pass": len(passed) / n * 100, "breach": sum(r.outcome == "breach" for r in runs) / n * 100,
            "median_days": float(np.median([r.days for r in passed])) if passed else float("nan")}


def run_static(days: list[DayResult], size: float, targets: tuple[float, ...], daily_pct: float, max_pct: float,
               start: int, *, max_days: int | None = None, payout_every: int | None = None,
               split: float = 0.80) -> AccountRun:
    """A prop account with FIXED limits (FTMO-style): fail if a day's worst moment is daily_pct below
    that day's starting balance, or max_pct below the starting size. targets: % per phase (empty =
    funded: withdraw all profit every `payout_every` trading days)."""
    run = AccountRun("end", 0)
    stop = len(days) if max_days is None else min(len(days), start + max_days)
    phase = 0
    balance = size
    last_pay = start
    for i in range(start, stop):
        d = days[i]
        if d.worst < -daily_pct / 100 * size or balance + d.worst < size * (1 - max_pct / 100):
            run.outcome, run.days, run.end_balance = "breach", i - start + 1, balance + d.worst
            return run
        balance += d.pnl
        if targets and balance >= size * (1 + targets[phase] / 100) - 1e-6:
            phase += 1
            balance = size
            if phase == len(targets):
                run.outcome, run.days, run.end_balance = "pass", i - start + 1, balance
                return run
        if not targets and payout_every and i - last_pay >= payout_every and balance - size >= 100:
            run.payouts.append((i, (balance - size) * split))
            balance = size
            last_pay = i
    run.days = stop - start
    run.end_balance = balance
    return run
