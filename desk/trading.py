"""The trading desk: the two loops that run the bot.

FAST TICK (every schedule.fast_check_seconds, default 60s, no AI):
  CoinSpot prices -> close-all request? -> code exits on every open position
  (stop-loss / trailing stop / take-profit / max hold) -> equity snapshot ->
  daily loss limit check -> daily summary -> heartbeat

AGENT CYCLE (every schedule.agent_cycle_minutes, default 15 min):
  Market Scanner -> news refresh -> News Analyst (only coins that need it) ->
  Exit Agent + Skeptic for held coins showing warning signs ->
  Entry debates for flagged coins (only if new trades are allowed at all,
  so no AI money is spent on trades that couldn't happen) ->
  Risk Manager -> execute -> log everything

The database is the bot's memory: positions, cash, switches and daily numbers
all live there, so a restart picks up exactly where it left off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from desk.agents.exit_agent import HeldPosition, needs_review
from desk.agents.news_analyst import CoinNews, NewsAnalyst
from desk.clock import Clock, iso, parse_iso
from desk.config import Config, Secrets
from desk.controls import ControlPanel, ControlState
from desk.data.market import MarketData, MarketSnapshot
from desk.db import Database
from desk.debate import Debate
from desk.exchange.coinspot import ApiHealth, CoinSpotError
from desk.executor import Executor
from desk.exits import check_exit
from desk.housekeeping import run_daily as run_housekeeping
from desk.llm import LLMClient
from desk.news import NewsFeed
from desk.notify import LogNotifier, Notifier
from desk.portfolio import Portfolio
from desk.risk import Controls, MarketState, RiskManager, record_decision
from desk.risk_inputs import entry_request, market_state
from desk.scanner import CoinScan, Scanner
from desk.trend_scanner import TrendScanner

log = logging.getLogger("desk.trading")

TREND_SIGNAL_MAX_AGE_HOURS = 12     # a daily signal older than this (bot was off) is skipped, not chased


class StartupRefused(Exception):
    """A safety check failed; the bot must not run."""


@dataclass
class CycleReport:
    cycle_id: str
    flagged: list[str] = field(default_factory=list)
    skipped_entries: str | None = None
    debates: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"cycle {self.cycle_id}"]
        parts.append(f"flagged: {', '.join(self.flagged) or 'none'}")
        if self.skipped_entries:
            parts.append(f"entries skipped ({self.skipped_entries})")
        parts += self.debates + [f"OPENED {x}" for x in self.opened] + [f"CLOSED {x}" for x in self.closed]
        parts += self.notes
        return " | ".join(parts)


class TradingDesk:
    def __init__(self, *, config: Config, secrets: Secrets, clock: Clock, db: Database, market: MarketData,
                 scanner: Scanner, news: NewsFeed, llm: LLMClient, broker, api_health: ApiHealth,
                 notifier: Notifier | None = None, coinspot=None, candles=None):
        self.config = config
        self.strategy = config.strategy.mode
        self.trend_scanner = TrendScanner(config, candles if candles is not None else getattr(scanner, "candles", None),
                                          clock)
        self.secrets = secrets
        self.clock = clock
        self.db = db
        self.market = market
        self.scanner = scanner
        self.news = news
        self.llm = llm
        self.api_health = api_health
        self.coinspot = coinspot
        self.mode = broker.mode
        self.notify = notifier or LogNotifier()
        self.controls = ControlPanel(db, clock)
        self.portfolio = Portfolio(db, clock, config, self.mode)
        self.executor = Executor(db, clock, config, broker, self.portfolio, self.notify)
        self.risk = RiskManager(config)
        self.debate = Debate(config, llm, db, clock)
        self.analyst = NewsAnalyst(config, news, llm, db, clock)
        self._last_news_fetch: datetime | None = None
        self._last_budget_warning: str | None = None

    @classmethod
    def from_app(cls, app, broker, notifier: Notifier | None = None) -> TradingDesk:
        return cls(config=app.config, secrets=app.secrets, clock=app.clock, db=app.db, market=app.market,
                   scanner=app.scanner, news=app.news, llm=app.llm, broker=broker,
                   api_health=app.coinspot.health, notifier=notifier, coinspot=app.coinspot, candles=app.candles)

    # =====================================================================
    # Start-up
    # =====================================================================
    def startup(self) -> list[str]:
        """Safety checks + crash recovery. Returns warnings; raises StartupRefused if unsafe."""
        warnings: list[str] = []
        if self.secrets.has_full_key and self.coinspot is not None:
            probe = self.coinspot.withdrawal_probe()
            if probe.enabled is True:
                raise StartupRefused("The CoinSpot full-access key in .env CAN WITHDRAW FUNDS. Delete that key "
                                     "on CoinSpot and make a new one with withdrawals switched off.")
            if probe.enabled is None:
                warnings.append(f"Couldn't confirm the CoinSpot key has withdrawals off ({probe.detail})")
        if not self.llm.available:
            warnings.append("No ANTHROPIC_API_KEY: the AI agents are off, so the bot will manage open positions "
                            "but will NOT open new ones.")
        warnings += self.executor.recover()
        open_pos = self.portfolio.open_positions()
        restarts = int(self.db.kv_get("restart_count", "0")) + 1
        self.db.kv_set("restart_count", str(restarts))
        msg = (f"Bot started in {self.mode.upper()} mode (start #{restarts}). "
               f"{len(open_pos)} open position(s) recovered from the database: "
               f"{', '.join(p.coin for p in open_pos) or 'none'}. Switches: {self.controls.state().describe()}")
        self.db.log_event("startup", msg, data={"warnings": warnings})
        self.notify.send("startup", msg)
        for w in warnings:
            log.warning(w)
        return warnings

    # =====================================================================
    # Fast tick: protect positions, every minute
    # =====================================================================
    def fast_tick(self) -> None:
        now = self.clock.now()
        try:
            snap = self.market.snapshot()
        except CoinSpotError as exc:
            self.db.log_event("api_error", f"Price check failed: {exc.message}", level="WARNING")
            log.warning("Price check failed: %s (CoinSpot errors in a row: %d)", exc.message,
                        self.api_health.consecutive_errors)
            if self.api_health.consecutive_errors == self.config.data.api_error_streak_limit:
                self.notify.send("error", f"CoinSpot isn't answering ({exc.message}). New trades are blocked; "
                                          f"stops can't run until it's back.")
            self._heartbeat("fast")
            return

        ctl = self.controls.state()
        if ctl.close_all_requested:
            self._close_all(snap, "kill_close" if ctl.killed else "manual_close")

        for pos in self.portfolio.open_positions():
            q = snap.quotes.get(pos.coin)
            if q is None:
                log.warning("No CoinSpot price for held coin %s; can't check its stop this minute", pos.coin)
                continue
            chk = check_exit(pos, q.bid, now, self.config.exits)
            if chk.highest_price != pos.highest_price or chk.trailing_active != pos.trailing_active:
                self.portfolio.update_exit_tracking(pos.id, chk.highest_price, chk.trailing_active)
                if chk.trailing_active and not pos.trailing_active:
                    self.notify.send("info", f"{pos.coin}: trailing stop is now active at A${chk.effective_stop:,.6g}")
            if chk.action == "SELL":
                self._sell(pos, snap, chk.reason, detail=chk.detail)

        st = self.portfolio.state(snap.quotes)
        self.portfolio.snapshot_equity(snap.quotes, st)
        if self.risk.daily_loss_hit(st) and not ctl.daily_paused:
            self.controls.set_daily_pause(self.clock.local_date())
            self.notify.send("risk_limit", f"Daily loss limit hit ({st.daily_pnl_pct:+.2f}% today, limit "
                                           f"-{self.config.risk.daily_loss_limit_pct}%). No new trades until "
                                           f"midnight. Open positions keep their stops.")
        self._maybe_daily_summary(st)
        try:
            run_housekeeping(self.db, self.clock)        # once a day: delete old scanner/price/news rows
        except Exception:
            log.exception("Daily database clean-up failed (will retry tomorrow)")
        self._heartbeat("fast")

    def _sell(self, pos, snap: MarketSnapshot, reason: str, *, detail: str = "", skeptic_vote: str | None = None,
              cycle_id: str | None = None, debate_id: int | None = None) -> bool:
        q = snap.quotes[pos.coin]
        mkt = MarketState(pos.coin, q.bid, q.ask, (self.clock.now() - q.fetched_at).total_seconds(),
                          api_error_streak=self.api_health.consecutive_errors)
        d = self.risk.check_exit(pos, pos.qty, mkt, reason=reason, skeptic_vote=skeptic_vote)
        record_decision(self.db, self.clock, d, cycle_id=cycle_id, debate_id=debate_id, reason=reason)
        if not d.approved:
            log.warning("Exit %s %s refused: %s", pos.coin, reason, "; ".join(d.reasons))
            return False
        return self.executor.close_position(pos, d.qty, reason, detail=detail, cycle_id=cycle_id) is not None

    def _close_all(self, snap: MarketSnapshot, reason: str) -> None:
        left = []
        for pos in self.portfolio.open_positions():
            if pos.coin not in snap.quotes or not self._sell(pos, snap, reason):
                left.append(pos.coin)
        if left:
            self.notify.send("error", f"Close-all: couldn't sell {', '.join(left)} yet; will retry next minute.")
        else:
            self.controls.clear_close_all()
            self.notify.send("info", "Close-all done: no open positions.")

    # =====================================================================
    # Agent cycle: find and judge trades, every 15 minutes
    # =====================================================================
    def agent_cycle(self) -> CycleReport:
        if self.strategy == "trend":
            return self.trend_cycle()
        now = self.clock.now()
        rep = CycleReport(cycle_id=now.strftime("%Y%m%dT%H%M%SZ"))
        try:
            snap = self.market.snapshot()
        except CoinSpotError as exc:
            rep.notes.append(f"no CoinSpot prices ({exc.message}); cycle skipped")
            self._heartbeat("cycle")
            return rep
        scans = {r.coin: r for r in self.scanner.scan(snap, cycle_id=rep.cycle_id)}
        rep.flagged = [c for c, r in scans.items() if r.flagged]
        self._refresh_news(now)

        ctl = self.controls.state()
        ai_ok, why_no_ai = self._ai_status(ctl)
        held = self.portfolio.open_positions()
        held_coins = {p.coin for p in held}

        st = self.portfolio.state(snap.quotes)
        gate = self._entry_gate(ctl, st)
        entry_candidates = [c for c in rep.flagged if c not in held_coins and not self.news.active_blocks(c)]
        if gate:
            rep.skipped_entries = gate
            entry_candidates = []
        elif rep.flagged and not ai_ok:
            rep.skipped_entries = why_no_ai
            entry_candidates = []

        review_coins = [p.coin for p in held if self._held_needs_ai(p.coin, scans.get(p.coin))]
        news: dict[str, CoinNews] = {}
        if ai_ok and (entry_candidates or review_coins):
            news = self.analyst.assess(list(dict.fromkeys(entry_candidates + review_coins)), cycle_id=rep.cycle_id)

        # ---- exits first: protecting what we hold matters more than new trades
        if ai_ok and review_coins:
            self._review_exits(held, snap, scans, news, rep)

        # ---- entries
        for coin in entry_candidates:
            ctl = self.controls.state()
            st = self.portfolio.state(snap.quotes)
            gate = self._entry_gate(ctl, st)
            if gate:
                rep.notes.append(f"stopped looking for entries: {gate}")
                break
            if news.get(coin) and news[coin].blocked:
                rep.debates.append(f"{coin}: blocked by a major news event")
                continue
            self._try_entry(coin, scans[coin], news.get(coin), snap, ctl, rep)

        self._heartbeat("cycle")
        self.db.log_event("cycle", rep.line())
        log.info(rep.line())
        return rep

    # =====================================================================
    # Trend cycle: once a day, after the daily candle closes
    # =====================================================================
    def trend_cycle(self) -> CycleReport:
        now = self.clock.now()
        rep = CycleReport(cycle_id="trend-" + now.strftime("%Y%m%dT%H%M%SZ"))
        try:
            snap = self.market.snapshot()
        except CoinSpotError as exc:
            rep.notes.append(f"no CoinSpot prices ({exc.message}); cycle skipped")
            self._heartbeat("cycle")
            return rep
        scans = {s.coin: s for s in self.trend_scanner.scan(snap)}
        rep.flagged = [c for c, s in scans.items() if s.flagged]
        for c, s in scans.items():
            if s.problems:
                rep.notes.append(f"{c}: {'; '.join(s.problems)}")
        self._refresh_news(now)

        ctl = self.controls.state()
        ai_ok, why_no_ai = self._ai_status(ctl)
        held = self.portfolio.open_positions()
        held_coins = {p.coin for p in held}
        st = self.portfolio.state(snap.quotes)

        candidates = []
        for c in rep.flagged:
            s = scans[c]
            key = f"trend_done:{c}:{s.candle_close_time.isoformat()}"
            age_h = (now - s.candle_close_time).total_seconds() / 3600
            if self.db.kv_get(key):
                continue                                   # this daily signal was already handled
            if c in held_coins:
                rep.debates.append(f"{c}: signal, but already holding it")
            elif age_h > TREND_SIGNAL_MAX_AGE_HOURS:
                rep.debates.append(f"{c}: signal missed (the bot was off at the daily close, {age_h:.0f}h ago)")
            elif self.news.active_blocks(c):
                rep.debates.append(f"{c}: signal, but blocked by a major news event")
            else:
                candidates.append(c)
                continue
            self.db.kv_set(key, "skipped")
        gate = self._entry_gate(ctl, st)
        if candidates and (gate or not ai_ok):
            rep.skipped_entries = gate or why_no_ai
            candidates = []

        review = [p.coin for p in held if self._held_needs_ai(p.coin, None)]
        news: dict[str, CoinNews] = {}
        if ai_ok and (candidates or review):
            news = self.analyst.assess(list(dict.fromkeys(candidates + review)), cycle_id=rep.cycle_id)
        if ai_ok and review:
            self._review_exits(held, snap, scans, news, rep)

        for coin in candidates:
            s = scans[coin]
            self.db.kv_set(f"trend_done:{coin}:{s.candle_close_time.isoformat()}", "debated")
            ctl = self.controls.state()
            gate = self._entry_gate(ctl, self.portfolio.state(snap.quotes))
            if gate:
                rep.notes.append(f"stopped looking for entries: {gate}")
                break
            if news.get(coin) and news[coin].blocked:
                rep.debates.append(f"{coin}: blocked by a major news event")
                continue
            self._try_entry(coin, s, news.get(coin), snap, ctl, rep)

        self._heartbeat("cycle")
        self.db.log_event("cycle", rep.line())
        log.info(rep.line())
        return rep

    def _try_entry(self, coin: str, scan, cn: CoinNews | None, snap: MarketSnapshot,
                   ctl: ControlState, rep: CycleReport) -> None:
        r = self.debate.run_entry(scan, cn, self.portfolio.open_positions(), cycle_id=rep.cycle_id)
        if r.outcome != "approved":
            rep.debates.append(f"{coin}: {r.outcome}")
            return
        st = self.portfolio.state(snap.quotes)
        controls = Controls(killed=ctl.killed, paused=ctl.paused, daily_loss_paused=ctl.daily_paused,
                            event_blocks=tuple(b["summary"] for b in self.news.active_blocks(coin)),
                            live=self.mode == "live", live_capital_cap_aud=self.config.live.max_capital_aud)
        d = self.risk.check_entry(entry_request(r.proposal, r.verdict["vote"]), st,
                                  market_state(scan, self.clock.now(), self.api_health.consecutive_errors), controls)
        record_decision(self.db, self.clock, d, cycle_id=rep.cycle_id, debate_id=r.id)
        if d.daily_limit_hit:
            self.controls.set_daily_pause(self.clock.local_date())
        if not d.approved:
            rep.debates.append(f"{coin}: agents agreed, Risk Manager said no ({', '.join(c.name for c in d.failures)})")
            self.notify.send("risk_veto", f"{coin} buy vetoed by the Risk Manager: " + "; ".join(d.reasons))
            return
        sig = getattr(scan, "signal", None)      # trend trades carry their own trailing stop and max hold
        pos = self.executor.open_position(d, cycle_id=rep.cycle_id, debate_id=r.id,
                                          trail_pct=sig.trail_pct if sig else None,
                                          max_hold_hours=sig.max_hold_hours if sig else None,
                                          strategy=self.strategy)
        if pos:
            rep.opened.append(f"{coin} {pos.qty:.8g} @ {pos.entry_price:,.6g}")

    def _review_exits(self, held, snap: MarketSnapshot, scans, news, rep: CycleReport) -> None:
        now = self.clock.now()
        items = []
        for p in held:
            q = snap.quotes.get(p.coin)
            if q is None:
                continue
            chk = check_exit(p, q.bid, now, self.config.exits)
            items.append(HeldPosition(p, q.bid, scans.get(p.coin), news.get(p.coin),
                                      chk.effective_stop, chk.trailing_active))
        by_key = {h.key: h for h in items}
        for r in self.debate.run_exits(items, now, cycle_id=rep.cycle_id):
            h = by_key[r.position_key]
            if r.outcome == "tighten" and r.new_stop:
                self.portfolio.set_stop(h.position.id, r.new_stop)
                rep.debates.append(f"{h.position.coin}: stop raised to {r.new_stop:,.6g}")
                self.notify.send("info", f"{h.position.coin}: Exit Agent raised the stop to A${r.new_stop:,.6g}")
            elif r.outcome == "approved":
                if self._sell(h.position, snap, "agent_sell", skeptic_vote=r.verdict["vote"],
                              cycle_id=rep.cycle_id, debate_id=r.id):
                    rep.closed.append(f"{h.position.coin} (Exit Agent + Skeptic)")
            else:
                rep.debates.append(f"{h.position.coin} exit review: {r.outcome}")

    # =====================================================================
    # Helpers
    # =====================================================================
    def _entry_gate(self, ctl: ControlState, st) -> str | None:
        """Cheap checks BEFORE spending money on AI: is a new trade even possible?"""
        r = self.config.risk
        if ctl.killed:
            return "kill switch on"
        if ctl.paused:
            return "paused"
        if ctl.daily_paused or self.risk.daily_loss_hit(st):
            return "daily loss limit hit"
        if len(st.open_positions) >= r.max_open_positions:
            return f"{len(st.open_positions)} positions open (max {r.max_open_positions})"
        if st.entries_today >= r.max_entries_per_day:
            return f"{st.entries_today} trades today (max {r.max_entries_per_day})"
        return None

    def _ai_status(self, ctl: ControlState) -> tuple[bool, str]:
        if ctl.killed:
            return False, "kill switch on"
        if not self.llm.available:
            return False, "no ANTHROPIC_API_KEY"
        if self.llm.budget_left_usd() <= 0.0:
            today = self.clock.local_date().isoformat()
            if self._last_budget_warning != today:
                self._last_budget_warning = today
                self.notify.send("risk_limit", "Daily AI budget used up: no new trades until midnight "
                                               "(stops still protect open positions).")
            return False, "daily AI budget used up"
        return True, ""

    def _held_needs_ai(self, coin: str, scan: CoinScan | None) -> bool:
        if scan is not None and scan.alerts:
            return True
        if self.news.active_blocks(coin):
            return True
        recent = self.news.recent(coin, hours=self.config.schedule.agent_cycle_minutes / 60 * 2)
        return any(i.event_kinds for i in recent)

    def _refresh_news(self, now: datetime) -> None:
        due = self._last_news_fetch is None or \
            now - self._last_news_fetch >= timedelta(minutes=self.config.schedule.news_refresh_minutes) - timedelta(seconds=30)
        if not due:
            return
        rep = self.news.fetch_all()
        self._last_news_fetch = now
        for b in rep.exchange_blocks:
            self.notify.send("risk_limit", f"ALL new trades blocked: {b}")

    def _heartbeat(self, which: str) -> None:
        self.db.kv_set(f"heartbeat_{which}", iso(self.clock.now()))

    def last_heartbeat(self, which: str) -> datetime | None:
        v = self.db.kv_get(f"heartbeat_{which}")
        return parse_iso(v) if v else None

    # ---- daily summary ---------------------------------------------------------
    def _maybe_daily_summary(self, st) -> None:
        local = self.clock.local_now()
        if local.strftime("%H:%M") < self.config.schedule.daily_summary_time:
            return
        key = f"daily_summary_sent:{local.date().isoformat()}"
        if self.db.kv_get(key):
            return
        self.db.kv_set(key, "1")
        self.notify.send("daily_summary", self.daily_summary_text(st))

    def daily_summary_text(self, st) -> str:
        since = iso(self.clock.day_start_utc())
        trades = self.db.query("SELECT side, gain_aud FROM trades WHERE mode = ? AND ts >= ?", (self.mode, since))
        buys = sum(1 for t in trades if t["side"] == "buy")
        sells = [t for t in trades if t["side"] == "sell"]
        wins = sum(1 for t in sells if (t["gain_aud"] or 0) > 0)
        realized = sum(t["gain_aud"] or 0 for t in sells)
        vetoes = self.db.query_one("SELECT COUNT(*) FROM risk_decisions WHERE kind='entry' AND approved=0 AND ts >= ?",
                                   (since,))[0]
        pnl_aud = st.equity_aud - st.day_start_equity_aud
        return "\n".join([
            f"Daily summary {self.clock.local_date():%a %d %b %Y} ({self.mode.upper()})",
            f"Equity A${st.equity_aud:,.2f} ({pnl_aud:+,.2f} / {st.daily_pnl_pct:+.2f}% today)",
            f"Trades: {buys} buy(s), {len(sells)} sell(s), {wins} winning sell(s), realised A${realized:+,.2f}",
            f"Open positions: {', '.join(p.coin for p in st.open_positions) or 'none'}",
            f"Risk Manager vetoes today: {vetoes}",
            f"AI spend today: US${self.llm.spent_today_usd():.4f}",
            f"Switches: {self.controls.state().describe()}",
        ])
