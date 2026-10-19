"""The desk's money: cash, open positions, equity and today's numbers.

Everything is worked out from the database every time it's asked for
(cash = starting balance - every buy + every sell), so there is no separate
balance that could drift out of sync after a crash or restart.
"""

from __future__ import annotations

from datetime import date

from desk.clock import Clock, iso, parse_iso
from desk.config import Config
from desk.db import Database
from desk.exchange.coinspot import Quote
from desk.models import Position
from desk.risk import PortfolioState

K_START = "paper_starting_balance_aud"


def _row_to_position(r) -> Position:
    return Position(
        id=r["id"], coin=r["coin"], qty=r["qty"], entry_price=r["entry_price"], stop_loss=r["stop_loss"],
        take_profit=r["take_profit"], opened_at=parse_iso(r["opened_at"]), highest_price=r["highest_price"],
        entry_fee_aud=r["entry_fee_aud"], trailing_active=bool(r["trailing_active"]), mode=r["mode"],
        trail_pct=r["trail_pct"], max_hold_hours=r["max_hold_hours"],
    )


class Portfolio:
    def __init__(self, db: Database, clock: Clock, config: Config, mode: str = "paper"):
        self.db = db
        self.clock = clock
        self.config = config
        self.mode = mode

    # ---- balances --------------------------------------------------------
    def starting_balance(self) -> float:
        """Fixed the first time the paper account is used, so editing config.yaml later
        can't silently rewrite history. (To change it, start a fresh database.)"""
        v = self.db.kv_get(K_START)
        if v is None:
            v = str(self.config.paper.starting_balance_aud)
            self.db.kv_set(K_START, v)
        return float(v)

    def cash_aud(self) -> float:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(CASE WHEN side='sell' THEN aud_amount - fee_aud ELSE 0 END), 0) AS sold, "
            "COALESCE(SUM(CASE WHEN side='buy' THEN aud_amount + fee_aud ELSE 0 END), 0) AS bought "
            "FROM trades WHERE mode = ?", (self.mode,))
        return self.starting_balance() + row["sold"] - row["bought"]

    def open_positions(self) -> list[Position]:
        rows = self.db.query("SELECT * FROM positions WHERE mode = ? AND status = 'open' ORDER BY id", (self.mode,))
        return [_row_to_position(r) for r in rows]

    def get_position(self, pid: int) -> tuple[Position, str] | None:
        r = self.db.query_one("SELECT * FROM positions WHERE id = ?", (pid,))
        return (_row_to_position(r), r["status"]) if r else None

    def positions_value(self, quotes: dict[str, Quote]) -> tuple[float, list[str]]:
        """Open positions valued at the CoinSpot BID (what we'd get selling now).
        Coins with no price right now are valued at their entry price and reported."""
        total, missing = 0.0, []
        for p in self.open_positions():
            q = quotes.get(p.coin)
            if q is None:
                missing.append(p.coin)
                total += p.qty * p.entry_price
            else:
                total += p.qty * q.bid
        return total, missing

    def equity(self, quotes: dict[str, Quote]) -> float:
        return self.cash_aud() + self.positions_value(quotes)[0]

    # ---- today -----------------------------------------------------------
    def day_start_equity(self, equity_now: float, today: date | None = None) -> float:
        """Equity at the first price check of the local day (saved, so restarts keep it)."""
        today = today or self.clock.local_date()
        key = f"day_start_equity:{self.mode}:{today.isoformat()}"
        v = self.db.kv_get(key)
        if v is None:
            equity_now = float(equity_now)            # plain float (numpy numbers repr() as "np.float64(...)")
            self.db.kv_set(key, repr(equity_now))
            return equity_now
        return float(v)

    def entries_today(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) FROM positions WHERE mode = ? AND opened_at >= ?",
                                (self.mode, iso(self.clock.day_start_utc())))
        return int(row[0])

    def realized_today_aud(self) -> float:
        row = self.db.query_one("SELECT COALESCE(SUM(gain_aud), 0) FROM trades WHERE mode = ? AND side = 'sell' "
                                "AND ts >= ?", (self.mode, iso(self.clock.day_start_utc())))
        return float(row[0])

    def state(self, quotes: dict[str, Quote]) -> PortfolioState:
        open_pos = self.open_positions()
        cash = self.cash_aud()
        equity = cash + self.positions_value(quotes)[0]
        live_exposure = sum(p.qty * p.entry_price + p.entry_fee_aud for p in open_pos) if self.mode == "live" else 0.0
        return PortfolioState(
            equity_aud=equity, cash_aud=cash, day_start_equity_aud=self.day_start_equity(equity),
            open_positions=tuple(open_pos), entries_today=self.entries_today(), live_exposure_aud=live_exposure,
        )

    # ---- position tracking ---------------------------------------------------
    def update_exit_tracking(self, pid: int, highest: float, trailing_active: bool) -> None:
        self.db.execute("UPDATE positions SET highest_price = ?, trailing_active = ? WHERE id = ? AND status = 'open'",
                        (highest, int(trailing_active), pid))

    def set_stop(self, pid: int, new_stop: float) -> None:
        """Raise a stop. Never lowers it (checked again here as a last line of defence)."""
        self.db.execute("UPDATE positions SET stop_loss = ? WHERE id = ? AND status = 'open' AND stop_loss < ?",
                        (new_stop, pid, new_stop))

    def snapshot_equity(self, quotes: dict[str, Quote], state: PortfolioState | None = None) -> None:
        st = state or self.state(quotes)
        value = st.equity_aud - st.cash_aud
        pnl = st.daily_pnl_pct
        self.db.execute(
            "INSERT INTO equity_snapshots(ts, mode, equity_aud, cash_aud, positions_value_aud, day_pnl_pct, "
            "open_positions) VALUES (?,?,?,?,?,?,?)",
            (iso(self.clock.now()), self.mode, st.equity_aud, st.cash_aud, value,
             pnl if pnl == pnl else None, len(st.open_positions)),
        )
