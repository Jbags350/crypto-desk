"""Exit Agent (AI part). The code part (stops, trailing stop, take-profit,
max hold) lives in desk/exits.py and runs every minute without any AI.

The AI is only asked about a position when there's something to judge:
the scanner shows warning signs for that coin, or the news turned clearly
negative or reported a major event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from desk.agents.base import Agent, AgentReply
from desk.agents.context import market_context, position_context, position_key
from desk.agents.news_analyst import CoinNews
from desk.models import Position
from desk.scanner import CoinScan
from desk.schemas import ExitReview

NEGATIVE_NEWS = -0.5


@dataclass
class HeldPosition:
    position: Position
    bid: float
    scan: CoinScan | None = None
    news: CoinNews | None = None
    effective_stop: float | None = None     # from the code exit check (includes the trailing stop)
    trailing_active: bool | None = None

    @property
    def key(self) -> str:
        return position_key(self.position)


def needs_review(h: HeldPosition) -> bool:
    if h.scan is not None and h.scan.alerts:
        return True
    if h.news is not None and (h.news.major_event or h.news.blocked or h.news.sentiment <= NEGATIVE_NEWS):
        return True
    return False


class ExitAgent(Agent):
    name = "exit_agent"
    prompt_file = "exit"

    def build_message(self, held: list[HeldPosition], now: datetime) -> str:
        blocks = [f"Review these {len(held)} open position(s). Answer for every position id."]
        for h in held:
            parts = [position_context(h.position, h.bid, now, self.config, h.effective_stop, h.trailing_active)]
            if h.scan is not None:
                parts.append(market_context(h.scan, h.news, self.config))
            blocks.append("\n".join(parts))
        return "\n\n---\n\n".join(blocks)

    def review(self, held: list[HeldPosition], now: datetime, cycle_id: str | None = None) -> AgentReply[ExitReview]:
        coins = ",".join(sorted({h.position.coin for h in held}))
        return self.ask(self.build_message(held, now), ExitReview, coin=coins, cycle_id=cycle_id)
