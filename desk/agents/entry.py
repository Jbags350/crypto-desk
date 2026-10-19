"""Entry Agent (AI): proposes a buy with entry, stop-loss, take-profit, reasoning, confidence."""

from __future__ import annotations

from desk.agents.base import Agent, AgentReply
from desk.agents.context import desk_rules, market_context, portfolio_line
from desk.agents.news_analyst import CoinNews
from desk.config import Config
from desk.models import Position
from desk.scanner import CoinScan
from desk.schemas import EntryProposal


def sanity_problems(p: EntryProposal, scan: CoinScan, config: Config) -> list[str]:
    """Basic checks that a BUY proposal makes sense at all. The full rule set is the Risk Manager's job."""
    problems = []
    if p.coin != scan.coin:
        problems.append(f"proposal is for {p.coin}, but the setup was for {scan.coin}")
    if not (0 < p.stop_loss_aud < p.entry_aud < p.take_profit_aud):
        problems.append("prices must satisfy 0 < stop-loss < entry < take-profit "
                        f"(got stop {p.stop_loss_aud:g}, entry {p.entry_aud:g}, take-profit {p.take_profit_aud:g})")
    if scan.ask is None:
        problems.append("no CoinSpot ask price to buy at")
    elif p.entry_aud > 0:
        gap = abs(p.entry_aud / scan.ask - 1) * 100
        if gap > config.risk.max_entry_gap_pct:
            problems.append(f"entry {p.entry_aud:g} is {gap:.2f}% from the CoinSpot ask {scan.ask:g} "
                            f"(max {config.risk.max_entry_gap_pct}%); the desk only buys at market")
    return problems


class EntryAgent(Agent):
    name = "entry_agent"
    prompt_file = "entry"

    def build_message(self, scan: CoinScan, news: CoinNews | None, open_positions: list[Position]) -> str:
        return "\n\n".join([
            "Should the desk buy this coin now?",
            market_context(scan, news, self.config),
            portfolio_line(scan.coin, open_positions, self.config),
            desk_rules(self.config),
        ])

    def propose(self, scan: CoinScan, news: CoinNews | None, open_positions: list[Position],
                cycle_id: str | None = None) -> AgentReply[EntryProposal]:
        return self.ask(self.build_message(scan, news, open_positions), EntryProposal,
                        coin=scan.coin, cycle_id=cycle_id)


class TrendEntryAgent(EntryAgent):
    """Trend mode: the rule picks the levels; the agent approves (BUY) or vetoes (NO_TRADE)."""

    prompt_file = "entry_trend"

    def build_message(self, scan, news: CoinNews | None, open_positions: list[Position]) -> str:
        return "\n\n".join([
            "The trend rule has signalled a breakout. Should the desk buy?",
            market_context(scan, news, self.config),
            portfolio_line(scan.coin, open_positions, self.config),
            desk_rules(self.config),
        ])

    def propose(self, scan, news: CoinNews | None, open_positions: list[Position],
                cycle_id: str | None = None) -> AgentReply[EntryProposal]:
        reply = super().propose(scan, news, open_positions, cycle_id)
        levels = scan.levels() if hasattr(scan, "levels") else None
        if reply.ok and reply.output.decision == "BUY" and levels:
            entry, stop, tp = levels      # the tested rule's levels, whatever the AI wrote
            reply.output = reply.output.model_copy(update={"entry_aud": entry, "stop_loss_aud": stop,
                                                           "take_profit_aud": tp})
        return reply
