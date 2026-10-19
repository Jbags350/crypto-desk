"""Telegram: alerts to your phone, and commands from it.

Alerts: every trade, risk-limit hits, errors, start-ups, the daily summary.
Commands (only from YOUR chat, TELEGRAM_CHAT_ID; everyone else is ignored):
  /status              equity, today's P&L, switches, last price check
  /positions           open positions with live prices and P&L
  /pause               no new trades (open positions still managed)
  /resume              allow new trades again
  /kill                stop trading and AI; stop-losses still protect positions
  /kill close          ...and sell everything (asks you to /confirm within 60s)
  /resume confirm      lift a kill
  /help                this list

Messages go out from a background queue, so a slow or unreachable Telegram
never holds up trading. Failed sends are retried, then dropped (and logged).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from desk.notify import Notifier

log = logging.getLogger("desk.telegram")

API = "https://api.telegram.org"
MAX_LEN = 4000          # Telegram's limit is 4096 characters per message

ICONS = {"trade": "💱", "risk_limit": "🛑", "risk_veto": "🚫", "error": "⚠️", "order_failed": "⚠️",
         "startup": "🟢", "daily_summary": "📊", "info": "ℹ️"}


class TelegramError(Exception):
    pass


class TelegramClient:
    """The two Bot API calls we need. The token is only ever in the URL, which the log filter masks."""

    def __init__(self, token: str, http: httpx.Client | None = None):
        self._token = token
        self._http = http or httpx.Client(timeout=40.0)

    def __repr__(self) -> str:
        return "TelegramClient(token=***)"

    def _call(self, method: str, http_timeout: float = 20.0, **params) -> dict | list:
        # http_timeout is OUR network timeout; "timeout" in params is Telegram's long-poll setting
        try:
            r = self._http.post(f"{API}/bot{self._token}/{method}", json=params, timeout=http_timeout)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"couldn't reach Telegram ({type(exc).__name__})") from None
        if not data.get("ok"):
            retry = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError(f"Telegram said no: {data.get('description', r.status_code)}"
                                + (f" (retry after {retry}s)" if retry else ""))
        return data["result"]

    def get_me(self) -> dict:
        return self._call("getMe")

    def send(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", chat_id=chat_id, text=text[:MAX_LEN], disable_web_page_preview=True)

    def updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", http_timeout=timeout + 15, **params)


class TelegramNotifier(Notifier):
    """Queues alerts and sends them from a background thread."""

    def __init__(self, client: TelegramClient, chat_id: str, *, retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep, start: bool = True):
        self.client = client
        self.chat_id = chat_id
        self.retries = retries
        self._sleep = sleep
        self._q: queue.Queue[str] = queue.Queue(maxsize=200)
        self.sent = 0
        self.dropped = 0
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._worker, name="telegram-send", daemon=True)
            self._thread.start()

    def send(self, kind: str, text: str) -> None:
        msg = f"{ICONS.get(kind, '•')} {text}"
        try:
            self._q.put_nowait(msg)
        except queue.Full:
            self.dropped += 1
            log.warning("Telegram queue full; alert dropped")

    def flush(self) -> None:
        """Send everything queued right now (used by tests and on shutdown)."""
        while True:
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                return
            self._deliver(msg)

    def _worker(self) -> None:
        while True:
            self._deliver(self._q.get())

    def _deliver(self, msg: str) -> None:
        for attempt in range(self.retries):
            try:
                self.client.send(self.chat_id, msg)
                self.sent += 1
                return
            except TelegramError as exc:
                log.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                self._sleep(min(30, 2 ** attempt * 2))
        self.dropped += 1


class MultiNotifier(Notifier):
    """Send every alert to several places (log + Telegram). Channels can be added while running."""

    def __init__(self, *notifiers: Notifier):
        self.notifiers = list(notifiers)

    def add(self, notifier: Notifier) -> None:
        self.notifiers.append(notifier)

    def send(self, kind: str, text: str) -> None:
        for n in self.notifiers:
            try:
                n.send(kind, text)
            except Exception:         # one broken channel must not break the others
                log.exception("Notifier %s failed", type(n).__name__)

    def flush(self) -> None:
        """Deliver anything still queued (on shutdown, so the last message isn't lost)."""
        for n in self.notifiers:
            flush = getattr(n, "flush", None)
            if flush:
                try:
                    flush()
                except Exception:
                    log.exception("Notifier %s failed to flush", type(n).__name__)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

HELP = """Crypto Desk commands:
/status – equity, today's P&L, switches
/positions – open positions with live P&L
/pause – no new trades (positions still managed)
/resume – allow new trades again
/kill – stop trading + AI (stops still protect positions)
/kill close – stop AND sell everything (asks to /confirm)
/resume confirm – lift a kill
/help – this list"""

CONFIRM_SECONDS = 60


class CommandHandler:
    """Turns a command message into a reply. Pure logic, easy to test."""

    def __init__(self, controls, status_fn: Callable[[], str], positions_fn: Callable[[], str],
                 now: Callable[[], datetime]):
        self.controls = controls
        self.status_fn = status_fn
        self.positions_fn = positions_fn
        self.now = now
        self._pending_close_until: datetime | None = None

    def handle(self, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return HELP
        cmd = parts[0].lower().split("@")[0]          # "/status@MyBot" in group chats
        arg = " ".join(parts[1:]).lower()
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return self.status_fn()
        if cmd == "/positions":
            return self.positions_fn()
        if cmd == "/pause":
            return self.controls.pause(by="telegram")
        if cmd == "/resume":
            ok, msg = self.controls.resume(by="telegram", confirm=(arg == "confirm"))
            return msg if ok else msg.replace("resume with confirm", "send /resume confirm")
        if cmd == "/kill":
            if arg in ("close", "close all", "all"):
                self._pending_close_until = self.now() + timedelta(seconds=CONFIRM_SECONDS)
                return (f"This will STOP trading and SELL ALL open positions. "
                        f"Send /confirm within {CONFIRM_SECONDS} seconds to go ahead.")
            return self.controls.kill(by="telegram")
        if cmd == "/confirm":
            if self._pending_close_until and self.now() <= self._pending_close_until:
                self._pending_close_until = None
                return self.controls.kill(by="telegram", close_all=True)
            self._pending_close_until = None
            return "Nothing to confirm (or it took longer than 60 seconds). Send /kill close again."
        return "I don't know that command.\n\n" + HELP


class TelegramCommands:
    """Background thread: long-polls Telegram for messages from your chat only."""

    def __init__(self, client: TelegramClient, chat_id: str, handler: CommandHandler, db=None,
                 sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.chat_id = str(chat_id)
        self.handler = handler
        self.db = db
        self._sleep = sleep
        self._offset: int | None = None
        self.stop = threading.Event()

    def poll_once(self, timeout: int = 25) -> int:
        """One long-poll. Returns how many commands were handled."""
        updates = self.client.updates(self._offset, timeout=timeout)
        handled = 0
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if chat != self.chat_id:
                log.warning("Ignored a Telegram message from another chat (%s)", chat)
                if self.db is not None:
                    self.db.log_event("telegram", f"Ignored a message from unknown chat {chat}", level="WARNING")
                continue
            if not text.startswith("/"):
                continue
            try:
                reply = self.handler.handle(text)
            except Exception as exc:
                log.exception("Telegram command %r failed", text)
                reply = f"Sorry, that command failed: {type(exc).__name__}"
            if self.db is not None:
                self.db.log_event("telegram", f"Command {text.split()[0]} from your chat")
            try:
                self.client.send(self.chat_id, reply)
            except TelegramError as exc:
                log.warning("Couldn't reply on Telegram: %s", exc)
            handled += 1
        return handled

    def skip_backlog(self) -> None:
        """Ignore commands sent while the bot was off (don't act on a stale /kill close)."""
        try:
            updates = self.client.updates(None, timeout=0)
            if updates:
                self._offset = updates[-1]["update_id"] + 1
        except TelegramError as exc:
            log.warning("Telegram: couldn't skip old messages: %s", exc)

    def run(self) -> None:
        self.skip_backlog()
        while not self.stop.is_set():
            try:
                started = time.monotonic()
                handled = self.poll_once()
                if handled == 0 and time.monotonic() - started < 1.0:
                    self._sleep(1.0)      # a long-poll should wait; if it didn't, never spin the CPU
            except TelegramError as exc:
                log.warning("Telegram polling problem: %s", exc)
                self._sleep(10)
            except Exception:
                log.exception("Telegram polling crashed; retrying in 30s")
                self._sleep(30)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="telegram-commands", daemon=True)
        t.start()
        return t
