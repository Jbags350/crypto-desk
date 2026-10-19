"""Executor: turns an APPROVED Risk Manager decision into a filled order and
updates the books, crash-safely.

Order of operations for every order:
  1. write the order to the database as an "intent"   (so a crash is visible)
  2. ask the broker to fill it
  3. in ONE database transaction: mark the order filled, record the trade,
     and open/update/close the position
If the bot dies between 1 and 3, the next start-up finds the leftover intent
and handles it (paper: marks it abandoned; live, Stage 9: checks CoinSpot).

A lock makes sure only one order is processed at a time, so the 1-minute stop
check and the 15-minute agent cycle can never trade the same position at once.
"""

from __future__ import annotations

import logging
import threading

from desk.broker import BrokerError, Fill
from desk.clock import Clock, iso
from desk.config import Config
from desk.db import Database
from desk.models import Position
from desk.notify import Notifier
from desk.portfolio import Portfolio
from desk.risk import RiskDecision

log = logging.getLogger("desk.executor")

DUST = 1e-8     # anything smaller than one satoshi counts as nothing left


class Executor:
    def __init__(self, db: Database, clock: Clock, config: Config, broker, portfolio: Portfolio,
                 notifier: Notifier):
        self.db = db
        self.clock = clock
        self.config = config
        self.broker = broker
        self.portfolio = portfolio
        self.notify = notifier
        self.mode = broker.mode
        self.lock = threading.RLock()

    # ---- orders table ------------------------------------------------------
    def _new_order(self, coin: str, side: str, qty: float, reason: str, limit: float | None,
                   position_id: int | None) -> int:
        now = iso(self.clock.now())
        cur = self.db.execute(
            "INSERT INTO orders(ts, updated_at, mode, coin, side, status, reason, requested_qty, limit_price, position_id) "
            "VALUES (?,?,?,?,?,'intent',?,?,?,?)", (now, now, self.mode, coin, side, reason, qty, limit, position_id))
        return int(cur.lastrowid)

    def _fail_order(self, order_id: int, status: str, error: str) -> None:
        self.db.execute("UPDATE orders SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                        (status, error, iso(self.clock.now()), order_id))

    def _fill_order(self, t, order_id: int, fill: Fill, position_id: int) -> None:
        t.execute("UPDATE orders SET status = ?, filled_qty = ?, avg_price = ?, notional_aud = ?, fee_aud = ?, "
                  "position_id = ?, detail = ?, updated_at = ? WHERE id = ?",
                  ("partial" if fill.partial else "filled", fill.qty, fill.avg_price, fill.notional_aud,
                   fill.fee_aud, position_id, fill.detail, iso(self.clock.now()), order_id))

    # ---- buying ------------------------------------------------------------
    def open_position(self, d: RiskDecision, *, cycle_id: str | None = None, debate_id: int | None = None,
                      trail_pct: float | None = None, max_hold_hours: float | None = None,
                      strategy: str | None = None) -> Position | None:
        if not d.approved or d.kind != "entry":
            raise ValueError("only approved entry decisions can be executed")
        with self.lock:
            # The Risk Manager sized the order at this price, so refusing anything dearer
            # guarantees the position can't exceed the per-trade limit.
            limit = d.max_price or d.price * (1 + self.config.risk.max_entry_gap_pct / 100)
            oid = self._new_order(d.coin, "buy", d.qty, "entry", limit, None)
            try:
                fill = self.broker.buy(d.coin, d.qty, max_price=limit)
            except BrokerError as exc:
                self._fail_order(oid, "rejected", str(exc))
                self.db.log_event("order", f"BUY {d.coin} not filled: {exc}", level="WARNING")
                self.notify.send("order_failed", f"BUY {d.coin} not filled: {exc}")
                return None
            now = self.clock.now()
            with self.db.tx() as t:
                cur = t.execute(
                    "INSERT INTO positions(mode, coin, status, qty, initial_qty, entry_price, entry_fee_aud, "
                    "stop_loss, take_profit, highest_price, trailing_active, opened_at, cycle_id, debate_id, "
                    "trail_pct, max_hold_hours, strategy) VALUES (?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.mode, d.coin, fill.qty, fill.qty, fill.avg_price, fill.fee_aud, d.stop_loss_aud,
                     d.take_profit_aud, fill.avg_price, int(trail_pct is not None), iso(now), cycle_id, debate_id,
                     trail_pct, max_hold_hours, strategy))
                pid = int(cur.lastrowid)
                self._fill_order(t, oid, fill, pid)
                t.execute("INSERT INTO trades(ts, mode, coin, side, qty, price, aud_amount, fee_aud, order_id, "
                          "position_id, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (iso(now), self.mode, d.coin, "buy", fill.qty, fill.avg_price, fill.notional_aud,
                           fill.fee_aud, oid, pid, "entry"))
            pos = self.portfolio.get_position(pid)[0]
            msg = (f"{self.mode.upper()} BUY {fill.qty:.8g} {d.coin} @ A${fill.avg_price:,.6g} = "
                   f"A${fill.notional_aud:,.2f} + fee A${fill.fee_aud:,.2f} | stop A${d.stop_loss_aud:,.6g} "
                   f"| target A${d.take_profit_aud:,.6g}" + (" (partial fill)" if fill.partial else ""))
            log.info(msg)
            self.db.log_event("trade", msg, data={"position_id": pid, "order_id": oid})
            self.notify.send("trade", msg)
            return pos

    # ---- selling -------------------------------------------------------------
    def close_position(self, pos: Position, qty: float, reason: str, *, detail: str = "",
                       cycle_id: str | None = None) -> Fill | None:
        with self.lock:
            current = self.portfolio.get_position(pos.id)
            if current is None or current[1] != "open":
                log.info("Position %s already closed; nothing to sell", pos.id)
                return None
            pos = current[0]
            qty = min(qty, pos.qty)
            oid = self._new_order(pos.coin, "sell", qty, reason, None, pos.id)
            try:
                fill = self.broker.sell(pos.coin, qty)
            except BrokerError as exc:
                self._fail_order(oid, "rejected", str(exc))
                self.db.log_event("order", f"SELL {pos.coin} ({reason}) not filled: {exc}", level="ERROR")
                self.notify.send("order_failed", f"SELL {pos.coin} ({reason}) NOT filled: {exc}. Will retry.")
                return None

            share = fill.qty / pos.qty
            fee_share = pos.entry_fee_aud * share
            cost_base = fill.qty * pos.entry_price + fee_share
            proceeds = fill.notional_aud - fill.fee_aud
            gain = proceeds - cost_base
            remaining = pos.qty - fill.qty
            now = self.clock.now()
            with self.db.tx() as t:
                self._fill_order(t, oid, fill, pos.id)
                t.execute("INSERT INTO trades(ts, mode, coin, side, qty, price, aud_amount, fee_aud, order_id, "
                          "position_id, reason, cost_base_aud, proceeds_aud, gain_aud, acquired_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (iso(now), self.mode, pos.coin, "sell", fill.qty, fill.avg_price, fill.notional_aud,
                           fill.fee_aud, oid, pos.id, reason, cost_base, proceeds, gain, iso(pos.opened_at)))
                if remaining <= DUST:
                    t.execute("UPDATE positions SET status = 'closed', qty = 0, entry_fee_aud = 0, closed_at = ?, "
                              "exit_price = ?, exit_reason = ?, realized_pnl_aud = realized_pnl_aud + ? WHERE id = ?",
                              (iso(now), fill.avg_price, reason, gain, pos.id))
                else:
                    t.execute("UPDATE positions SET qty = ?, entry_fee_aud = ?, "
                              "realized_pnl_aud = realized_pnl_aud + ? WHERE id = ?",
                              (remaining, pos.entry_fee_aud - fee_share, gain, pos.id))
            pct = gain / cost_base * 100 if cost_base else 0.0
            msg = (f"{self.mode.upper()} SELL {fill.qty:.8g} {pos.coin} @ A${fill.avg_price:,.6g} ({reason}) "
                   f"| P&L A${gain:+,.2f} ({pct:+.2f}% after fees)"
                   + (f" | {remaining:.8g} left (partial fill, will retry)" if remaining > DUST else "")
                   + (f" | {detail}" if detail else ""))
            log.info(msg)
            self.db.log_event("trade", msg, data={"position_id": pos.id, "order_id": oid, "gain_aud": gain})
            self.notify.send("trade", msg)
            return fill

    # ---- start-up recovery -----------------------------------------------------
    def recover(self) -> list[str]:
        """Handle orders left half-done by a crash. Paper fills are instant, so an
        'intent' with no fill means the bot died before filling: nothing happened."""
        notes = []
        for r in self.db.query("SELECT * FROM orders WHERE status = 'intent' AND mode = ?", (self.mode,)):
            self._fail_order(r["id"], "abandoned", "bot stopped before this order was filled")
            note = f"order {r['id']} ({r['side']} {r['coin']}) was interrupted by a restart and never filled"
            notes.append(note)
            self.db.log_event("recovery", note, level="WARNING", data=dict(r))
        return notes
