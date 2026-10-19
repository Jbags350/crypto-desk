"""The prop-account trading engine. PropEngine.minute() runs once a minute.

Every minute (per account):
  - stop orders that filled are recorded (paper: triggered here)
  - phase target reached  -> close everything, stop trading that account
  - daily brake hit (-2% since the day started) -> close everything, rest
    until the firm's 22:00 UTC day reset
Every hour (after each candle closes): trailing stops move up; equity saved.
22:00 UTC: the firm's day starts. New daily reference, brake lifted, swap
  (paper), funded payout reminder, daily summary.
00:00 UTC (the rebalance candle): the rotation. Coins that dropped out of
  the top are sold; the new strongest are bought after the AI review, sized so
  a stop-out loses risk_pct of the account (less when near the loss floor).
Switches (pause / kill / close-all) are shared with Telegram and control.bat.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from desk.clock import UTC, Clock, iso, parse_iso
from desk.config import Config
from desk.controls import ControlPanel, ControlState
from desk.db import Database
from desk.notify import Notifier
from desk.prop.brokers import AccountSnapshot, Fill, PropBroker, round_qty
from desk.prop.live_signals import LiveSignals, RotationDecision
from desk.prop.review import PropReviewer
from desk.prop.rules import PropRules

log = logging.getLogger("desk.prop.engine")

LATE_LIMIT = timedelta(hours=3)        # a rebalance more than 3h late is skipped (prices have moved on)
MIN_NOTIONAL = 50.0                    # US$: smaller orders aren't worth placing
MAX_HOLD = timedelta(days=30)


def last_boundary(now: datetime, hour: int) -> datetime:
    b = now.astimezone(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return b if b <= now else b - timedelta(days=1)


@dataclass
class Note:
    account: str
    coin: str
    side: int
    qty: float
    entry_price: float
    opened_at: datetime
    stop: float
    trail_dist: float | None
    best: float
    stop_ref: str | None


class PropEngine:
    def __init__(self, config: Config, rules: PropRules, broker: PropBroker, signals: LiveSignals, db: Database,
                 clock: Clock, notify: Notifier, controls: ControlPanel, reviewer: PropReviewer | None = None,
                 before_rebalance=None, sleep=time.sleep):
        self.config = config
        self.p = config.prop
        self.rules = rules
        self.broker = broker
        self.signals = signals
        self.db = db
        self.clock = clock
        self.notify = notify
        self.controls = controls
        self.reviewer = reviewer
        self.before_rebalance = before_rebalance       # e.g. refresh the news for the AI review
        self._sleep = sleep
        self.size = rules.account_size
        self.target_pct = None if self.p.phase == "funded" else rules.targets_pct[int(self.p.phase) - 1]
        self.accounts: list[str] = []
        self.last_snapshots: dict[str, AccountSnapshot] = {}

    # ---- database ---------------------------------------------------------------
    def _row(self, account: str):
        return self.db.query_one("SELECT * FROM prop_accounts WHERE account = ?", (account,))

    def _set(self, account: str, **cols) -> None:
        cols["updated_at"] = iso(self.clock.now())
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.db.execute(f"UPDATE prop_accounts SET {sets} WHERE account = ?", (*cols.values(), account))

    def notes(self, account: str) -> dict[str, Note]:
        out = {}
        for r in self.db.query("SELECT * FROM prop_positions WHERE account = ?", (account,)):
            out[r["coin"]] = Note(r["account"], r["coin"], r["side"], r["qty"], r["entry_price"],
                                  parse_iso(r["opened_at"]), r["stop"], r["trail_dist"], r["best"], r["stop_ref"])
        return out

    def _save_note(self, n: Note) -> None:
        self.db.execute("INSERT OR REPLACE INTO prop_positions(account, coin, side, qty, entry_price, opened_at, stop, "
                        "trail_dist, best, stop_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (n.account, n.coin, n.side, n.qty, n.entry_price, iso(n.opened_at), n.stop, n.trail_dist,
                         n.best, n.stop_ref))

    def _record_close(self, account: str, note: Note | None, fill: Fill, mark: float | None) -> float | None:
        """Write the finished trade and forget the note. Returns its P&L if known."""
        exit_px = fill.price if fill.price is not None else mark
        pnl = fill.pnl
        if pnl is None and note is not None and exit_px is not None:
            pnl = note.side * note.qty * (exit_px - note.entry_price)        # estimate (fees not included)
        entry = note.entry_price if note else 0.0
        opened = note.opened_at if note else self.clock.now()
        self.db.execute("INSERT INTO prop_trades(account, coin, side, qty, entry_price, exit_price, opened_at, "
                        "closed_at, pnl, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (account, fill.coin, fill.side, fill.qty, entry, exit_px, iso(opened), iso(self.clock.now()),
                         pnl, fill.reason or "closed"))
        self.db.execute("DELETE FROM prop_positions WHERE account = ? AND coin = ?", (account, fill.coin))
        return pnl

    # ---- start-up -----------------------------------------------------------------
    def startup(self) -> list[str]:
        warnings = []
        self.accounts = self.broker.accounts()
        for a in self.accounts:
            snap = self.broker.snapshot(a)
            self.last_snapshots[a] = snap
            row = self._row(a)
            if row is None:
                self.db.execute("INSERT INTO prop_accounts(account, broker, size, phase, day_ref, day_ref_at, "
                                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (a, self.broker.name, self.size, self.p.phase, max(snap.balance, snap.equity),
                                 iso(self.clock.now()), iso(self.clock.now())))
            elif row["phase"] != self.p.phase:
                self._set(a, phase=self.p.phase, target_hit_at=None, size=self.size,
                          day_ref=max(snap.balance, snap.equity), day_ref_at=iso(self.clock.now()))
                warnings.append(f"{a}: now trading phase {self.p.phase}")
            elif row["target_hit_at"]:
                warnings.append(f"{a}: phase {self.p.phase} target was reached on {row['target_hit_at'][:10]}; not "
                                f"trading it. Set prop.phase to the next phase in config.yaml when Tradeify moves you on.")
            notes = self.notes(a)
            for coin, n in notes.items():
                if coin not in snap.positions:
                    self._record_close(a, n, Fill(coin, n.side, n.qty, None, None, "closed_while_away"), None)
                    warnings.append(f"{a}: {coin} was closed while the bot was off (probably its stop-loss)")
            for coin in snap.positions:
                if coin not in notes:
                    warnings.append(f"{a}: {coin} position wasn't opened by this bot; it is left alone")
        if self.db.kv_get("prop_last_rebalance") is None:
            # don't act on a decision that is hours old: start with the next one
            b = last_boundary(self.clock.now(), self.p.strategy.rebalance_hour_utc)
            if self.clock.now() - b > LATE_LIMIT:
                self.db.kv_set("prop_last_rebalance", iso(b))
        if self.db.kv_get("prop_last_reset") is None:
            self.db.kv_set("prop_last_reset", iso(last_boundary(self.clock.now(), self.rules.daily_reset_hour_utc)))
        return warnings

    # ---- the minute -------------------------------------------------------------------
    def minute(self) -> None:
        now = self.clock.now()
        for f in self.broker.tick(now):
            self._stopped_out(f)
        ctl = self.controls.state()
        if ctl.close_all_requested:
            for a in self.accounts:
                self._flatten(a, "kill_close" if ctl.killed else "manual_close")
            self.controls.clear_close_all()
        for a in self.accounts:
            self._guard(a, now)
        self._maybe_reset(now)
        self._maybe_hourly(now)
        self._maybe_rebalance(now, ctl)
        self.db.kv_set("heartbeat_fast", iso(self.clock.now()))

    def _stopped_out(self, f: Fill) -> None:
        account = f.account
        note = self.notes(account).get(f.coin)
        if note is None:
            return
        pnl = self._record_close(account, note, f, None)
        self.notify.send("trade", f"{account}: {f.coin} stopped out at {f.price:,.6g} "
                                  f"(P&L ${pnl:+,.0f})" if pnl is not None else f"{account}: {f.coin} stopped out")

    def _respect_min_hold(self, opened_at: datetime) -> None:
        held = (self.clock.now() - opened_at).total_seconds()
        if held < self.rules.min_hold_seconds + 1:
            self._sleep(self.rules.min_hold_seconds + 1 - held)

    def _flatten(self, account: str, reason: str) -> float:
        notes = self.notes(account)
        for n in notes.values():
            self._respect_min_hold(n.opened_at)
        snap = self.last_snapshots.get(account)
        fills = self.broker.flatten(account, reason, {c: n.stop_ref for c, n in notes.items()})
        total = 0.0
        for f in fills:
            mark = snap.positions[f.coin].mark_price if snap and f.coin in snap.positions else None
            f.reason = f.reason or reason
            pnl = self._record_close(account, notes.get(f.coin), f, mark)
            total += pnl or 0.0
        return total

    def _guard(self, account: str, now: datetime) -> None:
        snap = self.broker.snapshot(account)
        self.last_snapshots[account] = snap
        row = self._row(account)
        notes = self.notes(account)
        for coin, n in notes.items():
            pos = snap.positions.get(coin)
            if pos is None:                                   # closed on the platform (its stop order)
                pnl = self._record_close(account, n, Fill(coin, n.side, n.qty, None, None, "stop"), n.stop)
                self.notify.send("trade", f"{account}: {coin} was closed by its stop-loss near {n.stop:,.6g}"
                                          + (f" (about ${pnl:+,.0f})" if pnl is not None else ""))
                continue
            if n.stop_ref is None:                            # its stop order didn't go in: keep trying
                try:
                    n.stop_ref = self.broker.move_stop(account, coin, n.stop, None)
                    self._save_note(n)
                    if n.stop_ref:
                        log.info("%s: protective stop for %s placed on retry", account, coin)
                except Exception as exc:
                    log.warning("%s: still can't place the stop for %s: %s", account, coin, exc)
            beyond = pos.mark_price <= n.stop if n.side > 0 else pos.mark_price >= n.stop
            if beyond:                                        # backup: the platform's stop should have fired
                self._close(account, n, "stop_backup")
                snap = self.broker.snapshot(account)
                self.last_snapshots[account] = snap
        if row["target_hit_at"]:
            return
        if self.target_pct is not None and snap.equity >= self.size * (1 + self.target_pct / 100):
            self._flatten(account, "target_reached")
            snap = self.broker.snapshot(account)
            self._set(account, target_hit_at=iso(now))
            self.notify.send("info", f"🎉 {account}: phase {self.p.phase} target reached! Balance ${snap.balance:,.2f} "
                                     f"(+{(snap.balance / self.size - 1) * 100:.1f}%). Everything is closed and this "
                                     f"account has stopped trading. When Tradeify moves you to the next phase, set "
                                     f"prop.phase in config.yaml and restart the bot.")
            return
        halted = row["halted_until"] and parse_iso(row["halted_until"]) > now
        if not halted and row["day_ref"] is not None:
            brake_level = row["day_ref"] - self.size * self.p.risk.daily_brake_pct / 100
            if snap.equity <= brake_level:
                had = bool(snap.positions)
                pnl = self._flatten(account, "daily_brake") if had else 0.0
                until = last_boundary(now, self.rules.daily_reset_hour_utc) + timedelta(days=1)
                self._set(account, halted_until=iso(until))
                self.notify.send("risk_limit", f"{account}: down {(row['day_ref'] - snap.equity) / self.size * 100:.1f}% "
                                               f"today, so the bot closed everything and rests until the firm's new day "
                                               f"(22:00 UTC). The firm's limit is {self.rules.daily_loss_pct:g}%.")

    # ---- 22:00 UTC: the firm's new day -----------------------------------------
    def _maybe_reset(self, now: datetime) -> None:
        b = last_boundary(now, self.rules.daily_reset_hour_utc)
        last = self.db.kv_get("prop_last_reset")
        if last and parse_iso(last) >= b:
            return
        for a in self.accounts:
            self.broker.daily_swap(a)
            snap = self.broker.snapshot(a)
            row = self._row(a)
            day_pnl = snap.equity - (row["day_ref"] or self.size)
            self._set(a, day_ref=max(snap.balance, snap.equity), day_ref_at=iso(now), halted_until=None)
            msg = (f"{a}: day closed {day_pnl:+,.2f} | equity ${snap.equity:,.2f} "
                   f"({(snap.equity / self.size - 1) * 100:+.2f}%) | {len(snap.positions)} open")
            if self.target_pct is not None and not row["target_hit_at"]:
                msg += f" | target +{self.target_pct:g}%, floor -{self.rules.max_loss_pct:g}%"
            self.notify.send("daily_summary", msg)
            if self.p.phase == "funded" and snap.balance - self.size >= self.p.payout_alert_usd:
                profit = snap.balance - self.size
                self.notify.send("info", f"💵 {a}: ${profit:,.0f} profit can be withdrawn (you'd get about "
                                         f"${profit * self.rules.profit_split:,.0f}). Request it on Tradeify's dashboard.")
        self.db.kv_set("prop_last_reset", iso(b))

    # ---- every hour: trailing stops ---------------------------------------------
    def _maybe_hourly(self, now: datetime) -> None:
        last = self.db.kv_get("prop_last_hour")
        if last and now - parse_iso(last) < timedelta(hours=1, seconds=30):
            return
        candle_close, closes = self.signals.last_closes(now)
        if candle_close is None or (last and parse_iso(last) >= candle_close):
            return
        for a in self.accounts:
            for coin, n in self.notes(a).items():
                c = closes.get(coin)
                if c is None:
                    continue
                if now - n.opened_at >= MAX_HOLD:
                    self._close(a, n, "time")
                    continue
                if n.trail_dist:
                    n.best = max(n.best, c) if n.side > 0 else min(n.best, c)
                    new_stop = n.best - n.side * n.trail_dist
                    better = new_stop > n.stop * 1.001 if n.side > 0 else new_stop < n.stop * 0.999
                    if better:
                        n.stop_ref = self.broker.move_stop(a, coin, new_stop, n.stop_ref)
                        n.stop = new_stop
                    self._save_note(n)
            snap = self.last_snapshots.get(a) or self.broker.snapshot(a)
            self.db.execute("INSERT INTO prop_equity(account, ts, balance, equity) VALUES (?, ?, ?, ?)",
                            (a, iso(now), snap.balance, snap.equity))
        self.db.kv_set("prop_last_hour", iso(candle_close))

    def _close(self, account: str, n: Note, reason: str) -> None:
        self._respect_min_hold(n.opened_at)
        snap = self.last_snapshots.get(account)
        mark = snap.positions[n.coin].mark_price if snap and n.coin in snap.positions else None
        fill = self.broker.close(account, n.coin, reason, n.stop_ref)
        if fill is None:
            self.db.execute("DELETE FROM prop_positions WHERE account = ? AND coin = ?", (account, n.coin))
            return
        fill.reason = reason
        pnl = self._record_close(account, n, fill, mark)
        self.notify.send("trade", f"{account}: sold {n.coin} ({reason})" + (f", P&L ${pnl:+,.0f}" if pnl is not None else ""))

    # ---- 00:00 UTC: the rotation ------------------------------------------------
    def _maybe_rebalance(self, now: datetime, ctl: ControlState) -> None:
        b = last_boundary(now, self.p.strategy.rebalance_hour_utc)
        last = self.db.kv_get("prop_last_rebalance")
        if (last and parse_iso(last) >= b) or now - b < timedelta(seconds=60):
            return
        if now - b > LATE_LIMIT:
            self.notify.send("error", "Today's rotation couldn't be worked out in time (price data missing?); "
                                      "skipped. Open positions keep their stops.")
            self.db.kv_set("prop_last_rebalance", iso(b))
            return
        if self.before_rebalance:
            try:
                self.before_rebalance()
            except Exception:
                log.exception("pre-rebalance step failed (continuing)")
        decision = self.signals.rotation(now)
        if decision is None or decision.candle_close < b:
            return                                         # candle not in yet: try again next minute
        self.rebalance(decision, ctl)
        self.db.kv_set("prop_last_rebalance", iso(b))
        self.db.kv_set("heartbeat_cycle", iso(self.clock.now()))

    def risk_for(self, equity: float) -> float:
        r = self.p.risk.risk_pct
        if self.p.risk.floor_scaling:
            cushion = (equity - self.rules.floor_usd) / (self.size - self.rules.floor_usd)
            r *= min(self.p.risk.scale_up_max, max(0.25, cushion))
        return r

    def rebalance(self, d: RotationDecision, ctl: ControlState) -> None:
        reviews: dict[str, bool] = {}
        summary = []
        for a in self.accounts:
            row = self._row(a)
            if row["target_hit_at"]:
                continue
            for coin, n in self.notes(a).items():
                if coin in d.sell:
                    self._close(a, n, "rotation")
                    summary.append(f"sold {coin}")
            halted = row["halted_until"] and parse_iso(row["halted_until"]) > self.clock.now()
            if halted or not ctl.entries_allowed:
                continue
            snap = self.broker.snapshot(a)
            self.last_snapshots[a] = snap
            held = set(snap.positions)
            used = sum(p.qty * p.mark_price / self.rules.lev(p.coin) for p in snap.positions.values())
            longs = sum(1 for p in snap.positions.values() if p.side > 0)
            for coin, _score in sorted(d.buy.items(), key=lambda kv: -kv[1]):
                if coin in held or len(held) >= self.p.risk.max_positions or longs >= self.p.risk.max_same_side:
                    continue
                if coin not in self.p.symbols or coin not in d.stop_dist:
                    continue
                if coin not in reviews and self.reviewer is not None:
                    reviews[coin], rv = self.reviewer.review(d.views[coin], d.stop_pct(coin), d.bull)
                    if rv is not None:
                        log.info("AI review %s: %s (%d) %s", coin, rv.verdict, rv.confidence, rv.reason)
                if not reviews.get(coin, True):
                    summary.append(f"{coin} vetoed by the AI review")
                    continue
                price = d.views[coin].close
                stop_pct = d.stop_pct(coin)
                risk = self.risk_for(snap.equity)
                qty = self.size * risk / 100 / (stop_pct * price)
                lev = self.rules.lev(coin)
                room = max(0.0, snap.equity * self.p.risk.margin_use - used) * lev / price
                step = self.broker.qty_step(coin) or self.p.quantity_step.get(coin, 0.0001)
                qty = round_qty(min(qty, room), step)
                if qty * price < MIN_NOTIONAL:
                    continue
                stop_guess = price * (1 - stop_pct)
                fill, stop_ref = self.broker.open(a, coin, 1, qty, stop_guess)
                entry = fill.price if fill.price is not None else price
                stop = entry * (1 - stop_pct)
                if abs(stop - stop_guess) / stop_guess > 0.002:          # re-anchor the stop to the real fill
                    stop_ref = self.broker.move_stop(a, coin, stop, stop_ref)
                self._save_note(Note(a, coin, 1, fill.qty, entry, self.clock.now(), stop, stop_pct * entry, entry,
                                     stop_ref))
                held.add(coin)
                longs += 1
                used += fill.qty * entry / lev
                summary.append(f"bought {coin} ({fill.qty:g} at {entry:,.6g}, stop {stop:,.6g}, risk {risk:.2f}%)")
                self.notify.send("trade", f"{a}: bought {fill.qty:g} {coin} at {entry:,.6g}; stop {stop:,.6g} "
                                          f"(-{stop_pct * 100:.1f}%), risking {risk:.2f}% of the account")
        regime = "Bitcoin above its 50-day average" if d.bull else "Bitcoin below its 50-day average: no buying"
        log.info("Rotation %s | %s | %s", d.candle_close.strftime("%Y-%m-%d %H:%M"), regime,
                 "; ".join(summary) or "no changes")
        self.db.log_event("prop_rotation", f"{regime}; " + ("; ".join(summary) or "no changes"))
