"""Skeptic (AI): argues against every proposal, then votes BUY/SELL, WAIT or SKIP."""

from __future__ import annotations

from datetime import datetime

from desk.agents.base import Agent, AgentReply
from desk.agents.context import aud, desk_rules, market_context, position_context, round_trip_cost_pct
from desk.agents.exit_agent import HeldPosition
from desk.agents.news_analyst import CoinNews
from desk.scanner import CoinScan
from desk.schemas import EntryProposal, ExitAdvice, SkepticVerdict


class Skeptic(Agent):
    name = "skeptic"
    prompt_file = "skeptic"

    def review_entry(self, p: EntryProposal, scan: CoinScan, news: CoinNews | None,
                     cycle_id: str | None = None) -> AgentReply[SkepticVerdict]:
        risk = p.entry_aud - p.stop_loss_aud
        reward = p.take_profit_aud - p.entry_aud
        rr = reward / risk if risk > 0 else float("nan")
        gain_pct = reward / p.entry_aud * 100 if p.entry_aud else float("nan")
        cost = round_trip_cost_pct(self.config, scan.spread_pct)
        multiple = f"{gain_pct / cost:.1f}x" if cost > 0 else "n/a (no costs)"
        if self.config.strategy.mode == "trend":
            levels = (f"Entry {aud(p.entry_aud)}, initial stop {aud(p.stop_loss_aud)} "
                      f"(-{risk / p.entry_aud * 100:.2f}%), which then trails up behind the price. No fixed "
                      f"take-profit (trend-following lets winners run).")
        else:
            levels = (f"Entry {aud(p.entry_aud)}, stop-loss {aud(p.stop_loss_aud)} "
                      f"(-{risk / p.entry_aud * 100:.2f}%), take-profit {aud(p.take_profit_aud)} (+{gain_pct:.2f}%)\n"
                      f"Reward:risk {rr:.2f}; take-profit gain is {multiple} the round-trip cost of {cost:.2f}%")
        msg = "\n\n".join([
            "PROPOSAL TO REVIEW: BUY",
            "\n".join([
                f"Coin: {p.coin}",
                levels,
                f"Entry Agent confidence: {p.confidence}/100",
                f"Entry Agent reasoning: {p.reasoning}",
            ]),
            "Market data the Entry Agent saw:",
            market_context(scan, news, self.config),
            desk_rules(self.config),
            "Argue against this buy, then vote BUY, WAIT or SKIP.",
        ])
        return self.ask(msg, SkepticVerdict, coin=p.coin, cycle_id=cycle_id)

    def review_exit(self, advice: ExitAdvice, held: HeldPosition, now: datetime,
                    cycle_id: str | None = None) -> AgentReply[SkepticVerdict]:
        parts = [
            "PROPOSAL TO REVIEW: SELL (close this position now)",
            f"Exit Agent confidence: {advice.confidence}/100\nExit Agent reasoning: {advice.reasoning}",
            "The position:",
            position_context(held.position, held.bid, now, self.config, held.effective_stop, held.trailing_active),
        ]
        if held.scan is not None:
            parts += ["Market data:", market_context(held.scan, held.news, self.config)]
        parts.append("Argue against selling now, then vote SELL, WAIT or SKIP.")
        return self.ask("\n\n".join(parts), SkepticVerdict, coin=held.position.coin, cycle_id=cycle_id)
