"""AI second opinion on each planned prop buy (prompts/prop_review.md).

Modes (config prop.ai_review):
  off     no AI calls
  shadow  the AI's verdict is recorded (prop_reviews table) but never blocks;
          later we can check whether its vetoes would have helped
  veto    a veto skips the buy
If the AI is unavailable or over its daily budget, the trade goes ahead (the
tested rule doesn't need the AI).
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from desk.clock import Clock, iso
from desk.db import Database
from desk.llm import LLMClient, LLMError
from desk.prompts import load_prompt
from desk.prop.live_signals import CoinView
from desk.schemas import PropReview

log = logging.getLogger("desk.prop.review")


def _neutralise(text: str) -> str:
    return text.replace("<", "(").replace(">", ")")


class PropReviewer:
    def __init__(self, mode: str, llm: LLMClient | None, db: Database, clock: Clock,
                 aliases: dict[str, list[str]] | None = None):
        self.mode = mode
        self.llm = llm
        self.db = db
        self.clock = clock
        self.aliases = aliases or {}

    def headlines(self, coin: str, hours: float = 36, limit: int = 12) -> list[str]:
        words = self.aliases.get(coin, [coin.lower()])
        pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
        since = iso(self.clock.now() - timedelta(hours=hours))
        rows = self.db.query("SELECT title, source, published_at FROM news_items WHERE published_at >= ? "
                             "ORDER BY published_at DESC LIMIT 400", (since,))
        out = []
        for r in rows:
            if pattern.search(r["title"]):
                out.append(f"[{r['published_at'][:16]} {r['source']}] {_neutralise(r['title'])}")
                if len(out) >= limit:
                    break
        return out

    def review(self, view: CoinView, stop_pct: float, bull: bool) -> tuple[bool, PropReview | None]:
        """(allowed, review). allowed is False only for a veto in veto mode."""
        if self.mode == "off" or self.llm is None or not self.llm.available:
            return True, None
        news = self.headlines(view.coin)
        user = (f"<planned_buy>\ncoin: {view.coin}\n2-week return: {view.ret * 100:+.1f}% (rank {view.rank} of 9)\n"
                f"last 24h: {view.change_24h_pct:+.1f}%\nprice vs 50-day average: {view.ema50_gap_pct:+.1f}%\n"
                f"daily range (ATR): {view.atr_pct:.1f}% of price\nstop-loss: {stop_pct * 100:.1f}% below\n"
                f"Bitcoin above its 50-day average: {'yes' if bull else 'no'}\n</planned_buy>\n"
                f"<headlines>\n" + ("\n".join(news) if news else "(none in the last 36 hours)") + "\n</headlines>")
        try:
            res = self.llm.call("prop_review", load_prompt("prop_review"), user, PropReview)
        except LLMError as exc:
            log.warning("AI review of %s unavailable (%s); the trade goes ahead", view.coin, exc)
            return True, None
        rv = res.output
        blocked = self.mode == "veto" and rv.verdict == "veto"
        self.db.execute("INSERT INTO prop_reviews(ts, coin, mode, verdict, confidence, reason, blocked) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (iso(self.clock.now()), view.coin, self.mode, rv.verdict, rv.confidence, rv.reason,
                         int(blocked)))
        return not blocked, rv
