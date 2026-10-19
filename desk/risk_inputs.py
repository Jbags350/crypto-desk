"""Turns scanner / agent / news outputs into Risk Manager inputs.

Kept separate from risk.py so the Risk Manager itself stays a set of pure
rules with no knowledge of where its numbers come from.
"""

from __future__ import annotations

from datetime import datetime

from desk.config import Config
from desk.risk import EntryRequest, MarketState, PortfolioState
from desk.scanner import CoinScan


def market_state(scan: CoinScan, now: datetime, api_error_streak: int = 0) -> MarketState:
    age = (now - scan.quote_at).total_seconds() if scan.quote_at else float("inf")
    return MarketState(
        coin=scan.coin,
        bid=scan.bid if scan.bid is not None else float("nan"),
        ask=scan.ask if scan.ask is not None else float("nan"),
        quote_age_seconds=age,
        data_problems=tuple(scan.problems),
        api_error_streak=api_error_streak,
    )


def entry_request(proposal: dict, skeptic_vote: str | None) -> EntryRequest:
    return EntryRequest(
        coin=proposal["coin"],
        entry_aud=float(proposal["entry_aud"]),
        stop_loss_aud=float(proposal["stop_loss_aud"]),
        take_profit_aud=float(proposal["take_profit_aud"]),
        confidence=float(proposal["confidence"]),
        skeptic_vote=skeptic_vote,
    )


def fresh_paper_portfolio(config: Config) -> PortfolioState:
    """A brand-new paper account: starting balance in cash, nothing open, nothing traded today."""
    start = config.paper.starting_balance_aud
    return PortfolioState(equity_aud=start, cash_aud=start, day_start_equity_aud=start)
