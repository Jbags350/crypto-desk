"""Futures contracts (micro versions) and Tradeify Select Flex account rules.

Contract values are CME's micro contracts. Costs are deliberately on the
pessimistic side: commission per round trip, plus one tick of slippage on every
market or stop fill (take-profit limit orders fill at their price).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Contract:
    code: str                  # our market code
    micro: str                 # the micro contract traded
    point_value: float         # US$ per 1.0 price move, per micro contract
    tick: float                # minimum price step
    session: tuple[str, str]   # main session, New York time
    commission: float = 1.50   # US$ per round trip per micro contract (all-in, conservative)


CONTRACTS = {
    "NQ": Contract("NQ", "MNQ", 2.0, 0.25, ("09:30", "16:00")),
    "ES": Contract("ES", "MES", 5.0, 0.25, ("09:30", "16:00")),
    "GC": Contract("GC", "MGC", 10.0, 0.10, ("08:20", "13:30")),
    "CL": Contract("CL", "MCL", 100.0, 0.01, ("09:00", "14:30")),
}


@dataclass(frozen=True)
class FuturesRules:
    """Tradeify Select Flex (checked Oct 2026; third-party summaries of the help centre).
    The loss limit trails the highest END-OF-DAY balance and stops trailing once it reaches
    the starting balance (+$100). It is checked live, so an open loss can breach it."""
    name: str
    size: float
    price_usd: float
    target: float              # evaluation profit target (US$)
    drawdown: float            # trailing max loss (US$)
    max_micros: int
    payout_cap: float          # per payout request
    payout_winning_days: int = 5
    winning_day_min: float = 200.0
    profit_split: float = 0.90
    max_accounts: int = 5
    eval_consistency: float = 0.40   # evaluation: best day <= 40% of the profit target

    @property
    def lock_level(self) -> float:
        return self.size + 100.0


def select_flex(size: int = 100_000) -> FuturesRules:
    # prices: Tradeify's pricing reference (Select evaluation, one-time; $169 / $239 to reset after a fail)
    if size == 150_000:
        return FuturesRules("Tradeify Select Flex 150K", 150_000, 369.0, 9_000, 4_500, 120, 5_000, winning_day_min=250)
    return FuturesRules("Tradeify Select Flex 100K", 100_000, 265.0, 6_000, 3_000, 80, 3_500)
