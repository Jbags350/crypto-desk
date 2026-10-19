"""Watchdog: tells you when the bot stops working.  python -m desk.watchdog

A separate small program (its own Docker container, or watchdog.bat on
Windows) that checks the bot's heartbeat in the database every minute:

  - no price check for watchdog.alert_after_minutes (10) -> Telegram alert,
    repeated every repeat_alert_minutes (60) while it's still down
  - when it comes back -> "running again" message
  - if you stopped the bot yourself (Ctrl+C / docker compose stop) -> one
    calm message, no repeated alarms
  - optional HEALTHCHECKS_URL (healthchecks.io): pinged while the bot is
    healthy. If the WHOLE server dies, these pings stop and healthchecks.io
    alerts you. That's the one failure nothing running on the server can report.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import timedelta

import httpx

from desk.clock import Clock, iso, parse_iso
from desk.config import Config, Secrets, load_config, load_secrets
from desk.db import Database
from desk.healthcheck import heartbeat_age_seconds
from desk.log_setup import setup_logging
from desk.telegram import TelegramClient, TelegramError

log = logging.getLogger("desk.watchdog")

K_DOWN_SINCE = "watchdog_down_since"
K_LAST_ALERT = "watchdog_last_alert"
K_STOPPED = "bot_stopped_cleanly_at"      # written by the bot on a clean shutdown


class Watchdog:
    def __init__(self, config: Config, secrets: Secrets, db: Database, clock: Clock,
                 send=None, http: httpx.Client | None = None, reload_secrets=None):
        self.config = config
        self.db = db
        self.clock = clock
        self.started = clock.now()
        self.healthchecks_url = secrets.healthchecks_url
        self.http = http or httpx.Client(timeout=15.0)
        self._reload_secrets = reload_secrets       # lets Telegram be set up while the watchdog runs
        self._send = send or self._telegram_sender(secrets)

    @staticmethod
    def _telegram_sender(secrets: Secrets):
        if not (secrets.telegram_bot_token and secrets.telegram_chat_id):
            return None
        client = TelegramClient(secrets.telegram_bot_token)
        return lambda text: client.send(secrets.telegram_chat_id, text)

    def notify(self, text: str) -> None:
        log.warning(text)
        self.db.log_event("watchdog", text, level="WARNING")
        if self._send is None and self._reload_secrets is not None:
            self._send = self._telegram_sender(self._reload_secrets())
        if self._send is None:
            return
        try:
            self._send(text)
        except TelegramError as exc:
            log.warning("Watchdog couldn't send to Telegram: %s", exc)

    def ping(self, ok: bool) -> None:
        if not self.healthchecks_url:
            return
        try:
            self.http.get(self.healthchecks_url + ("" if ok else "/fail"))
        except httpx.HTTPError as exc:
            log.warning("healthchecks.io ping failed: %s", type(exc).__name__)

    def check(self) -> str:
        """One check. Returns 'ok', 'down', 'stopped' or 'starting'."""
        now = self.clock.now()
        w = self.config.watchdog
        age = heartbeat_age_seconds(self.db, now)
        down_since = self.db.kv_get(K_DOWN_SINCE)
        stopped = self.db.kv_get(K_STOPPED)
        last_beat = self.db.kv_get("heartbeat_fast")

        limit = w.alert_after_minutes * 60
        if age is not None and age <= limit:
            if down_since:
                mins = (now - parse_iso(down_since)).total_seconds() / 60
                self.notify(f"✅ The bot is running again (it was down for about {mins:.0f} minutes).")
                self.db.kv_set(K_DOWN_SINCE, "")
                self.db.kv_set(K_LAST_ALERT, "")
            self.ping(True)
            return "ok"

        # Not cycling. Just started ourselves? Give the bot the same time to get going
        # (Docker starts both together), unless we already knew it was down.
        if not down_since and (now - self.started).total_seconds() <= limit:
            return "starting"

        # Did the user stop it on purpose (after its last heartbeat)?
        if stopped and last_beat and parse_iso(stopped) >= parse_iso(last_beat):
            if self.db.kv_get(K_LAST_ALERT) != f"stopped:{stopped}":
                self.notify("⏹ The bot was stopped by you (clean shutdown). No further alerts until it runs again.")
                self.db.kv_set(K_LAST_ALERT, f"stopped:{stopped}")
                self.db.kv_set(K_DOWN_SINCE, last_beat)
            self.ping(False)
            return "stopped"

        if not down_since:
            down_since = last_beat or iso(now)
            self.db.kv_set(K_DOWN_SINCE, down_since)
        last_alert = self.db.kv_get(K_LAST_ALERT)
        due = (not last_alert or last_alert.startswith("stopped:")
               or now - parse_iso(last_alert) >= timedelta(minutes=w.repeat_alert_minutes))
        if due:
            when = "never" if age is None else f"{age / 60:.0f} minutes ago"
            self.notify(f"🚨 The bot has stopped working: its last price check was {when}. While it's down, "
                        f"nothing is watching your stop-losses. Check the server / paper.bat window. "
                        f"(Next reminder in {w.repeat_alert_minutes:g} min if it's still down.)")
            self.db.kv_set(K_LAST_ALERT, iso(now))
        self.ping(False)
        return "down"

    def run_forever(self, sleep=time.sleep) -> None:
        log.info("Watchdog started (alert after %g min without a price check)", self.config.watchdog.alert_after_minutes)
        while True:
            try:
                self.check()
            except Exception:
                log.exception("Watchdog check failed")
            sleep(self.config.watchdog.check_every_seconds)


def main() -> int:
    config = load_config()
    secrets = load_secrets()
    setup_logging(config.path("logs"), process_name="watchdog", level=config.logging.level,
                  max_mb=config.logging.file_max_mb, backups=config.logging.file_backups,
                  secrets=secrets.secret_values())
    clock = Clock(config.tz)
    db = Database(config.path("database"), clock)
    db.migrate()
    if not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        log.warning("Telegram isn't set up, so the watchdog can only write to its log (run telegram_setup.bat)")
    Watchdog(config, secrets, db, clock, reload_secrets=load_secrets).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
