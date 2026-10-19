"""The debate: an agent proposes, the Skeptic argues against it and votes.

Entry debate:   Entry Agent -> (BUY?) -> sanity check -> Skeptic -> outcome
Exit review:    Exit Agent -> HOLD / TIGHTEN_STOP / SELL -> (SELL?) -> Skeptic -> outcome

Outcomes:
  approved  agents agree; goes on to the Risk Manager (who has the final veto)
  wait      Skeptic says "not yet"; looked at again next cycle
  rejected  Skeptic says no
  no_trade  Entry Agent chose not to buy
  hold      Exit Agent says keep the position
  tighten   Exit Agent raised the stop (protective only, so no Skeptic needed)
  invalid   proposal broke basic sanity rules (Skeptic not asked, which saves money)
  error     an AI call failed. Treated as "no", but code stops still protect positions.

Every debate, with the full transcript, is stored in the `debates` table.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from desk.agents.entry import EntryAgent, TrendEntryAgent, sanity_problems
from desk.agents.exit_agent import ExitAgent, HeldPosition, needs_review
from desk.agents.news_analyst import CoinNews
from desk.agents.skeptic import Skeptic
from desk.clock import Clock, iso
from desk.config import Config
from desk.db import Database
from desk.exits import validate_tighten
from desk.llm import LLMClient
from desk.models import Position
from desk.scanner import CoinScan

log = logging.getLogger("desk.debate")


@dataclass
class DebateResult:
    kind: str                     # entry | exit
    coin: str
    outcome: str
    proposal: dict | None = None
    verdict: dict | None = None
    problems: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    position_key: str | None = None
    new_stop: float | None = None
    id: int | None = None

    @property
    def approved(self) -> bool:
        return self.outcome == "approved"


ENTRY_VOTES = {"BUY": "approved", "WAIT": "wait", "SKIP": "rejected"}
EXIT_VOTES = {"SELL": "approved", "WAIT": "wait", "SKIP": "rejected"}


class Debate:
    def __init__(self, config: Config, llm: LLMClient, db: Database, clock: Clock):
        self.config = config
        self.db = db
        self.clock = clock
        agent_cls = TrendEntryAgent if config.strategy.mode == "trend" else EntryAgent
        self.entry_agent = agent_cls(config, llm, db, clock)
        self.exit_agent = ExitAgent(config, llm, db, clock)
        self.skeptic = Skeptic(config, llm, db, clock)

    # ---- entries -----------------------------------------------------------
    def run_entry(self, scan: CoinScan, news: CoinNews | None, open_positions: list[Position],
                  cycle_id: str | None = None) -> DebateResult:
        r = DebateResult(kind="entry", coin=scan.coin, outcome="error")
        reply = self.entry_agent.propose(scan, news, open_positions, cycle_id)
        if not reply.ok:
            r.problems.append(f"Entry Agent failed: {reply.error}")
            r.transcript.append({"agent": "entry_agent", "error": reply.error})
            return self._save(r, cycle_id)

        p = reply.output
        r.proposal = p.model_dump()
        r.transcript.append({"agent": "entry_agent", "output": r.proposal})
        if p.decision == "NO_TRADE":
            r.outcome = "no_trade"
            return self._save(r, cycle_id)

        r.problems = sanity_problems(p, scan, self.config)
        if r.problems:
            r.outcome = "invalid"
            return self._save(r, cycle_id)

        sk = self.skeptic.review_entry(p, scan, news, cycle_id)
        if not sk.ok:
            r.problems.append(f"Skeptic failed: {sk.error}")
            r.transcript.append({"agent": "skeptic", "error": sk.error})
            return self._save(r, cycle_id)
        r.verdict = sk.output.model_dump()
        r.transcript.append({"agent": "skeptic", "output": r.verdict})
        if sk.output.vote in ENTRY_VOTES:
            r.outcome = ENTRY_VOTES[sk.output.vote]
        else:
            r.outcome = "rejected"
            r.problems.append(f"Skeptic voted {sk.output.vote} on a buy proposal (counts as a no)")
        return self._save(r, cycle_id)

    # ---- exits -------------------------------------------------------------
    def run_exits(self, held: list[HeldPosition], now: datetime, cycle_id: str | None = None,
                  force: bool = False) -> list[DebateResult]:
        """Ask the Exit Agent about positions that need it (or all of them, if force=True)."""
        to_review = held if force else [h for h in held if needs_review(h)]
        if not to_review:
            return []
        reply = self.exit_agent.review(to_review, now, cycle_id)
        if not reply.ok:
            return [self._save(DebateResult(kind="exit", coin=h.position.coin, outcome="error",
                                            position_key=h.key, problems=[f"Exit Agent failed: {reply.error}"],
                                            transcript=[{"agent": "exit_agent", "error": reply.error}]), cycle_id)
                    for h in to_review]

        advice_by_key = {a.position_id: a for a in reply.output.positions}
        results = []
        for h in to_review:
            r = DebateResult(kind="exit", coin=h.position.coin, outcome="hold", position_key=h.key)
            a = advice_by_key.get(h.key)
            if a is None:
                r.problems.append("Exit Agent gave no advice for this position (keeping it)")
                results.append(self._save(r, cycle_id))
                continue
            r.proposal = a.model_dump()
            r.transcript.append({"agent": "exit_agent", "output": r.proposal})

            if a.action == "TIGHTEN_STOP":
                problem = validate_tighten(h.position, a.new_stop_aud, h.bid, self.config.exits)
                if problem:
                    r.problems.append(f"stop not changed: {problem}")
                else:
                    r.outcome, r.new_stop = "tighten", a.new_stop_aud
            elif a.action == "SELL":
                sk = self.skeptic.review_exit(a, h, now, cycle_id)
                if not sk.ok:
                    r.outcome = "error"
                    r.problems.append(f"Skeptic failed: {sk.error} (keeping the position)")
                    r.transcript.append({"agent": "skeptic", "error": sk.error})
                else:
                    r.verdict = sk.output.model_dump()
                    r.transcript.append({"agent": "skeptic", "output": r.verdict})
                    if sk.output.vote in EXIT_VOTES:
                        r.outcome = EXIT_VOTES[sk.output.vote]
                    else:
                        r.outcome = "rejected"
                        r.problems.append(f"Skeptic voted {sk.output.vote} on a sell proposal (counts as a no)")
            results.append(self._save(r, cycle_id))
        return results

    # ---- storage -------------------------------------------------------------
    def _save(self, r: DebateResult, cycle_id: str | None) -> DebateResult:
        cur = self.db.execute(
            "INSERT INTO debates(ts, cycle_id, kind, coin, position_key, outcome, proposal, verdict, problems, transcript) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iso(self.clock.now()), cycle_id, r.kind, r.coin, r.position_key, r.outcome,
             json.dumps(r.proposal) if r.proposal is not None else None,
             json.dumps(r.verdict) if r.verdict is not None else None,
             json.dumps(r.problems), json.dumps(r.transcript)),
        )
        r.id = int(cur.lastrowid)
        log.info("%s debate %s: %s%s", r.kind, r.coin, r.outcome, f" ({'; '.join(r.problems)})" if r.problems else "")
        return r
