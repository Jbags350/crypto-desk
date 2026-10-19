"""Run the trading bot: paper.bat  (or  python -m desk.main).

Two worker threads run the desk's loops; the main thread watches them:
  - an error inside a loop is logged and the loop carries on next time
  - if a loop stops making progress (hung), the whole process exits with
    code 3 so whatever started it (paper.bat, or Docker on a server)
    restarts it, and the database lets it carry on where it left off
Press Ctrl+C to stop cleanly. `docker compose stop` (SIGTERM) does the same,
and both leave a "stopped on purpose" note so the watchdog doesn't raise an alarm.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

from desk.bootstrap import build_app
from desk.broker.paper import PaperBroker
from desk.clock import Clock, iso
from desk.config import ConfigError, load_config, load_secrets
from desk.notify import LogNotifier
from desk.status import positions_text, status_text
from desk.strategy import paper_bot_supports
from desk.telegram import (CommandHandler, MultiNotifier, TelegramClient, TelegramCommands, TelegramError,
                           TelegramNotifier)
from desk.system import InstanceLock, keep_awake
from desk.trading import StartupRefused, TradingDesk

log = logging.getLogger("desk.main")

EXIT_OK, EXIT_REFUSED, EXIT_HUNG = 0, 2, 3
K_STOPPED = "bot_stopped_cleanly_at"      # the watchdog reads this


def stop_on_sigterm() -> None:
    """Docker stops a container with SIGTERM. Treat it like Ctrl+C so the bot shuts down cleanly."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)


def next_cycle_time(now: datetime, minutes: int, delay_seconds: int) -> datetime:
    """Next slot aligned to candle closes (counted from 00:00 UTC), plus a small delay.
    15 minutes -> :00:20, :15:20, :30:20, :45:20.   1440 minutes -> 00:01:00 UTC every day."""
    period = minutes * 60
    epoch = int(now.timestamp())
    t = datetime.fromtimestamp(epoch - epoch % period, now.tzinfo) + timedelta(seconds=delay_seconds)
    while t <= now:
        t += timedelta(seconds=period)
    return t


def cycle_schedule(config) -> tuple[int, int]:
    """(cycle minutes, delay seconds) for the strategy: trend mode runs once a day after the daily close."""
    if config.strategy.mode == "trend":
        return 1440, max(60, config.schedule.cycle_delay_seconds)
    return config.schedule.agent_cycle_minutes, config.schedule.cycle_delay_seconds


class Runner:
    def __init__(self, desk: TradingDesk, clock: Clock, fast_seconds: int, cycle_minutes: int, delay_seconds: int,
                 exit_fn=os._exit, watch_seconds: float = 10):
        self.desk = desk
        self._exit = exit_fn               # os._exit in real life; tests swap it out
        self.watch_seconds = watch_seconds
        self.clock = clock
        self.fast_seconds = fast_seconds
        self.cycle_minutes = cycle_minutes
        self.delay = delay_seconds
        self.stop = threading.Event()
        self.errors = {"fast": 0, "cycle": 0}

    def _guarded(self, name: str, fn) -> None:
        try:
            fn()
            self.errors[name] = 0
        except Exception as exc:     # a loop must never die from one bad iteration
            self.errors[name] += 1
            log.exception("%s loop error #%d", name, self.errors[name])
            self.desk.db.log_event("error", f"{name} loop error: {type(exc).__name__}: {exc}", level="ERROR")
            if self.errors[name] == 3:
                self.desk.notify.send("error", f"The {name} loop has failed 3 times in a row: {type(exc).__name__}: "
                                               f"{exc}. Still retrying; check logs/desk.log.")

    def fast_loop(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            self._guarded("fast", self.desk.fast_tick)
            self.stop.wait(max(1.0, self.fast_seconds - (time.monotonic() - started)))

    def cycle_loop(self) -> None:
        self._guarded("cycle", self.desk.agent_cycle)          # one cycle straight away at start-up
        while not self.stop.is_set():
            target = next_cycle_time(self.clock.now(), self.cycle_minutes, self.delay)
            log.info("Next agent cycle at %s", target.astimezone(self.clock.tz).strftime("%H:%M:%S"))
            while not self.stop.is_set() and self.clock.now() < target:
                self.stop.wait(min(5.0, max(0.1, (target - self.clock.now()).total_seconds())))
            if not self.stop.is_set():
                self._guarded("cycle", self.desk.agent_cycle)

    def run_forever(self) -> int:
        threads = [threading.Thread(target=self.fast_loop, name="fast", daemon=True),
                   threading.Thread(target=self.cycle_loop, name="cycle", daemon=True)]
        for t in threads:
            t.start()
        fast_limit = max(5 * self.fast_seconds, 300)
        cycle_limit = self.cycle_minutes * 60 * 3 + 300
        try:
            while not self.stop.is_set():
                self.stop.wait(self.watch_seconds)
                now = self.clock.now()
                for which, limit in (("fast", fast_limit), ("cycle", cycle_limit)):
                    hb = self.desk.last_heartbeat(which)
                    if hb is not None and (now - hb).total_seconds() > limit:
                        msg = f"The {which} loop has not finished for {(now - hb).total_seconds():.0f}s: restarting the bot"
                        log.critical(msg)
                        self.desk.db.log_event("watchdog", msg, level="CRITICAL")
                        self.desk.notify.send("error", msg)
                        self._exit(EXIT_HUNG)
                for t in threads:
                    if not t.is_alive() and not self.stop.is_set():
                        log.critical("The %s thread died; restarting the bot", t.name)
                        self._exit(EXIT_HUNG)
        except KeyboardInterrupt:
            self.shutdown(threads)
        return EXIT_OK

    def shutdown(self, threads) -> None:
        print("\nStopping... (finishing the current step)")
        self.stop.set()
        for t in threads:
            t.join(timeout=60)
        # Written after the loops have finished, so it's newer than their last heartbeat.
        self.desk.db.kv_set(K_STOPPED, iso(self.clock.now()))
        self.desk.db.log_event("shutdown", "Bot stopped by user (Ctrl+C or docker stop)")
        self.desk.notify.send("info", "Bot stopped on purpose (Ctrl+C, docker compose stop, or a server restart).")
        flush = getattr(self.desk.notify, "flush", None)
        if flush:
            flush()


def setup_telegram_alerts(app) -> tuple[TelegramNotifier | None, str]:
    """(notifier or None, a one-line description for the start-up banner)."""
    s = app.secrets
    if not s.telegram_bot_token:
        return None, "off (no TELEGRAM_BOT_TOKEN in .env; add one, then run telegram_setup.bat)"
    if not s.telegram_chat_id:
        return None, ("waiting: run telegram_setup.bat (Telegram switches on by itself within a minute, "
                      "no restart needed)")
    client = TelegramClient(s.telegram_bot_token)
    try:
        me = client.get_me()
    except TelegramError as exc:
        return None, f"off: {exc} (check TELEGRAM_BOT_TOKEN in .env)"
    return TelegramNotifier(client, s.telegram_chat_id), f"on (@{me.get('username', '?')}; alerts + commands)"


def watch_for_telegram(app, desk: TradingDesk, multi: MultiNotifier, stop: threading.Event,
                       every: float = 60.0, env_path=None) -> threading.Thread:
    """If Telegram isn't set up yet, re-read .env every minute. As soon as a token and chat ID
    appear (telegram_setup.bat writes them), switch alerts and commands on without a restart."""

    def loop():
        while not stop.wait(every):
            s = load_secrets(env_path)
            if not (s.telegram_bot_token and s.telegram_chat_id):
                continue
            client = TelegramClient(s.telegram_bot_token)
            try:
                me = client.get_me()
            except TelegramError as exc:
                log.warning("Telegram details found in .env but not working yet: %s", exc)
                continue
            tg = TelegramNotifier(client, s.telegram_chat_id)
            multi.add(tg)
            start_telegram_commands(app, desk, tg)
            tg.send("startup", f"Telegram connected to the running bot (@{me.get('username')}). Send /help for commands.")
            log.info("Telegram switched on while running")
            return

    t = threading.Thread(target=loop, name="telegram-watch", daemon=True)
    t.start()
    return t


def start_telegram_commands(app, desk: TradingDesk, notifier: TelegramNotifier):
    cfg, clock = app.config, app.clock

    def positions():
        try:
            quotes = app.coinspot.latest_all()
        except Exception:
            quotes = None
        return positions_text(app.db, clock, cfg, quotes)

    handler = CommandHandler(desk.controls, lambda: status_text(app.db, clock, cfg, compact=True),
                             positions, clock.now)
    return TelegramCommands(notifier.client, notifier.chat_id, handler, db=app.db).start()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the Crypto Desk trading bot (paper mode)")
    p.add_argument("--config", help="path to a config.yaml")
    p.add_argument("--once", action="store_true", help="run one price check and one agent cycle, then exit")
    args = p.parse_args(argv)
    try:
        if load_config(args.config).prop.enabled:                # prop firm mode replaces the CoinSpot bot
            from desk.prop.main import run as run_prop
            return run_prop((["--config", args.config] if args.config else []) + (["--once"] if args.once else []))
        app = build_app("desk", config_path=args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return EXIT_REFUSED

    if app.secrets.live_trading:
        print("LIVE_TRADING=true is set in .env, but live trading isn't built yet (Stage 9).\n"
              "Set LIVE_TRADING=false to run in paper mode.")
        return EXIT_REFUSED

    not_ready = paper_bot_supports(app.config)
    if not_ready:
        print(f"Can't start the bot with these settings: {not_ready}")
        return EXIT_REFUSED

    lock = InstanceLock(app.config.path("database").with_suffix(".lock"))
    if not lock.acquire():
        print("Another copy of the bot is already running on this database (check your other windows).\n"
              "Only one copy may run at a time, so this one is not starting.")
        return EXIT_REFUSED

    tg_notifier, tg_note = setup_telegram_alerts(app)
    notifier = MultiNotifier(LogNotifier(), tg_notifier) if tg_notifier else MultiNotifier(LogNotifier())
    desk = TradingDesk.from_app(app, PaperBroker(app.coinspot, app.config), notifier=notifier)
    try:
        warnings = desk.startup()
    except StartupRefused as exc:
        print(f"\nREFUSING TO START: {exc}")
        app.db.log_event("startup_refused", str(exc), level="CRITICAL")
        return EXIT_REFUSED

    cfg = app.config
    st = desk.portfolio
    cycle_minutes, cycle_delay = cycle_schedule(cfg)
    print("=" * 78)
    print(f" Crypto Desk - PAPER TRADING (fake money)   started {app.clock.local_now():%Y-%m-%d %H:%M %Z}")
    print("=" * 78)
    print(f"  Strategy: {cfg.strategy.mode.upper()}   |   max {cfg.risk.max_position_pct:g}% of the account per trade, "
          f"{cfg.risk.max_open_positions} positions")
    print(f"  Paper account: A${st.cash_aud():,.2f} cash, {len(st.open_positions())} open position(s)")
    if cfg.strategy.mode == "trend":
        first = next_cycle_time(app.clock.now(), cycle_minutes, cycle_delay).astimezone(cfg.tz)
        print(f"  Price checks every {cfg.schedule.fast_check_seconds}s; trend check once a day after the daily close "
              f"(next {first:%a %H:%M}); AI budget US${cfg.llm.daily_spend_cap_usd:.2f}/day")
    else:
        print(f"  Price checks every {cfg.schedule.fast_check_seconds}s; agent cycles every "
              f"{cycle_minutes} min; AI budget US${cfg.llm.daily_spend_cap_usd:.2f}/day")
    print(f"  Switches: {desk.controls.state().describe()}")
    print(f"  Telegram: {tg_note}")
    for w in warnings:
        print(f"  ! {w}")
    print("  Leave this window open. Press Ctrl+C to stop. Check progress any time with status.bat.")
    print("=" * 78)

    if args.once:
        desk.fast_tick()
        rep = desk.agent_cycle()
        print(rep.line())
        if tg_notifier:
            tg_notifier.flush()
        app.close()
        lock.release()
        return EXIT_OK

    stop_watch = threading.Event()
    if tg_notifier:
        start_telegram_commands(app, desk, tg_notifier)
    else:
        watch_for_telegram(app, desk, notifier, stop_watch)      # switches Telegram on once it's set up

    stop_on_sigterm()
    awake = cfg.schedule.keep_pc_awake and keep_awake(True)
    if awake:
        print("  This PC won't go to sleep while the bot runs (the screen can still turn off).")
    runner = Runner(desk, app.clock, cfg.schedule.fast_check_seconds, cycle_minutes, cycle_delay)
    try:
        code = runner.run_forever()
    finally:
        if awake:
            keep_awake(False)
        app.close()
        lock.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
