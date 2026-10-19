"""Replay a whole prop challenge from many start dates.

For each start date: Phase 1 (e.g. +10%), then Phase 2 (+5%) on a fresh
account, then the funded account, withdrawing profit every couple of weeks
until a limit is broken or the time runs out. The answer is how often the bot
passes, how long it takes, and what that's worth after the fee.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from desk.prop.market import Market
from desk.prop.rules import PropRules
from desk.prop.sim import SimConfig, simulate
from desk.prop.strategies import Signals


@dataclass
class Attempt:
    start_t: int
    phases: list[tuple[str, float]]        # (outcome, days) per phase
    passed: bool
    funded_days: float = 0.0
    funded_outcome: str = ""
    payouts: float = 0.0
    incomplete: bool = False               # ran out of price history before finishing

    @property
    def days_to_pass(self) -> float:
        return sum(d for _, d in self.phases)


def attempt(m: Market, sig: Signals, rules: PropRules, cfg: SimConfig, start_t: int, *,
            phase_max_days: float = 180, funded_days: float = 180, payout_every_days: int = 14) -> Attempt:
    phases = []
    t = start_t
    for target in rules.targets_pct:
        res = simulate(m, sig, rules, replace(cfg, target_pct=target, max_days=phase_max_days,
                                              payout_every_days=None, enforce_rules=True), t)
        phases.append((res.outcome, res.hours / 24))
        if res.outcome == "end":
            return Attempt(start_t, phases, False, incomplete=True)
        if res.outcome != "pass":
            return Attempt(start_t, phases, False)
        t = res.end_t + 1
        if t >= m.T - 24:
            return Attempt(start_t, phases, False, incomplete=True)
    res = simulate(m, sig, rules, replace(cfg, target_pct=None, max_days=funded_days,
                                          payout_every_days=payout_every_days, enforce_rules=True), t)
    return Attempt(start_t, phases, True, res.hours / 24, res.outcome, res.payouts,
                   incomplete=res.outcome == "end")


@dataclass
class ChallengeStats:
    attempts: int                          # complete attempts (ones cut off by the end of the data are left out)
    pass_rate: float
    median_days_to_pass: float
    fail_daily: int
    fail_max: int
    fail_timeout: int
    avg_payout_if_passed: float
    funded_breach_rate: float
    ev_per_attempt: float                  # average payout per attempt minus the fee

    def line(self) -> str:
        return (f"pass {self.pass_rate:5.1f}% of {self.attempts} | median {self.median_days_to_pass:5.0f} days | "
                f"fails: daily {self.fail_daily}, max {self.fail_max}, slow {self.fail_timeout} | "
                f"funded payout ${self.avg_payout_if_passed:,.0f} (breached {self.funded_breach_rate:.0f}%) | "
                f"EV ${self.ev_per_attempt:+,.0f}")


def evaluate(m: Market, sig: Signals, rules: PropRules, cfg: SimConfig, first_t: int, last_t: int,
             every_days: int = 7, **kw) -> tuple[ChallengeStats, list[Attempt]]:
    starts = range(first_t, last_t, every_days * 24)
    atts = [a for a in (attempt(m, sig, rules, cfg, s, **kw) for s in starts) if not a.incomplete]
    passed = [a for a in atts if a.passed]
    fails = [a.phases[-1][0] for a in atts if not a.passed]
    n = max(1, len(atts))
    avg_pay = float(np.mean([a.payouts for a in passed])) if passed else 0.0
    stats = ChallengeStats(
        attempts=len(atts),
        pass_rate=len(passed) / n * 100,
        median_days_to_pass=float(np.median([a.days_to_pass for a in passed])) if passed else float("nan"),
        fail_daily=fails.count("breach_daily"),
        fail_max=fails.count("breach_max"),
        fail_timeout=sum(1 for f in fails if f in ("timeout", "end")),
        avg_payout_if_passed=avg_pay,
        funded_breach_rate=(sum(1 for a in passed if a.funded_outcome.startswith("breach")) / len(passed) * 100
                            if passed else 0.0),
        ev_per_attempt=sum(a.payouts for a in atts) / n - rules.price_usd,
    )
    return stats, atts
