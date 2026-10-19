"""Pause / resume / kill switches, stored in the database.

Anything can flip a switch: control.bat, Telegram commands and the
dashboard. The running bot reads them on every tick.

  pause       no NEW trades. Open positions are still managed (stops, exits).
  resume      lifts a pause. Lifting a kill needs resume with confirm=True.
  kill        stop trading: no new trades and no AI calls. Code stop-losses,
              trailing stops and take-profits still protect open positions.
  close all   sell every open position at the next fast check (used by "kill close").
  daily pause set automatically when the daily loss limit is hit; it ends by
              itself at local midnight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from desk.clock import Clock
from desk.db import Database
from desk.risk import daily_pause_active

K_PAUSED = "ctl_paused"
K_KILLED = "ctl_killed"
K_DAILY = "ctl_daily_pause_date"
K_CLOSE_ALL = "ctl_close_all_requested"


@dataclass(frozen=True)
class ControlState:
    paused: bool
    killed: bool
    daily_paused: bool
    close_all_requested: bool

    @property
    def entries_allowed(self) -> bool:
        return not (self.paused or self.killed or self.daily_paused)

    def describe(self) -> str:
        if self.killed:
            return "KILLED (no new trades, no AI; stops still active)"
        parts = []
        if self.paused:
            parts.append("PAUSED (no new trades)")
        if self.daily_paused:
            parts.append("DAILY LOSS LIMIT HIT (no new trades until midnight)")
        return ", ".join(parts) or "RUNNING"


class ControlPanel:
    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock

    def _flag(self, key: str) -> bool:
        return self.db.kv_get(key, "0") == "1"

    def _set(self, key: str, on: bool) -> None:
        self.db.kv_set(key, "1" if on else "0")

    def state(self) -> ControlState:
        raw = self.db.kv_get(K_DAILY)
        paused_on = date.fromisoformat(raw) if raw else None
        return ControlState(
            paused=self._flag(K_PAUSED),
            killed=self._flag(K_KILLED),
            daily_paused=daily_pause_active(paused_on, self.clock.local_date()),
            close_all_requested=self._flag(K_CLOSE_ALL),
        )

    def _event(self, kind: str, message: str, by: str, level: str = "INFO") -> None:
        self.db.log_event(kind, message, level=level, data={"by": by})

    def pause(self, by: str = "unknown") -> str:
        self._set(K_PAUSED, True)
        self._event("control", "Trading paused: no new trades (open positions still managed)", by)
        return "Paused. No new trades; open positions are still protected by their stops."

    def resume(self, by: str = "unknown", confirm: bool = False) -> tuple[bool, str]:
        st = self.state()
        if st.killed and not confirm:
            return False, "The kill switch is on. To really restart trading, resume with confirm."
        self._set(K_PAUSED, False)
        if st.killed:
            self._set(K_KILLED, False)
        self._event("control", "Trading resumed" + (" (kill switch cleared)" if st.killed else ""), by)
        msg = "Resumed. New trades allowed again."
        if st.daily_paused:
            msg += " Note: the daily loss limit pause still lasts until midnight."
        return True, msg

    def kill(self, by: str = "unknown", close_all: bool = False) -> str:
        self._set(K_KILLED, True)
        if close_all:
            self._set(K_CLOSE_ALL, True)
        self._event("control", "KILL switch on" + (" + close all positions" if close_all else ""), by, "WARNING")
        return ("KILLED. No new trades and no AI calls. "
                + ("All positions will be sold at the next price check." if close_all
                   else "Open positions keep their stop-losses."))

    def request_close_all(self, by: str = "unknown") -> str:
        self._set(K_CLOSE_ALL, True)
        self._event("control", "Close-all requested", by, "WARNING")
        return "All positions will be sold at the next price check."

    def clear_close_all(self) -> None:
        self._set(K_CLOSE_ALL, False)

    def set_daily_pause(self, today: date) -> None:
        self.db.kv_set(K_DAILY, today.isoformat())
        self._event("risk_limit", f"Daily loss limit hit: new trades paused until midnight ({today})", "bot",
                    "WARNING")

    def daily_pause_date(self) -> str | None:
        return self.db.kv_get(K_DAILY)
