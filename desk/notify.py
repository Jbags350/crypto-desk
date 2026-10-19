"""Alerts: always to the log and the database; Telegram is added alongside
(desk.telegram.TelegramNotifier, combined with MultiNotifier)."""

from __future__ import annotations

import logging

from desk.db import Database

log = logging.getLogger("desk.alerts")


class Notifier:
    def send(self, kind: str, text: str) -> None:
        raise NotImplementedError


class LogNotifier(Notifier):
    """Writes every alert to the log and the events table."""

    def __init__(self, db: Database | None = None):
        self.db = db

    def send(self, kind: str, text: str) -> None:
        log.info("[alert:%s] %s", kind, text)
        if self.db is not None:
            self.db.log_event(f"alert_{kind}", text)


class MemoryNotifier(Notifier):
    """Keeps alerts in a list (used by tests)."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, kind: str, text: str) -> None:
        self.sent.append((kind, text))
