"""Run the prop-firm bot. Started by paper.bat / Docker (desk.main hands over
to this when prop.enabled is true in config.yaml), or: python -m desk.prop.main

One loop, once a minute (desk.prop.engine.PropEngine.minute). A guard thread
restarts the process (exit code 3) if the loop stops making progress, and
Ctrl+C / docker stop shut it down cleanly, like the CoinSpot bot.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

import httpx

from desk.bootstrap import USER_AGENT
from desk.clock import Clock, iso
from desk.config import ConfigError, load_config, load_secrets
from desk.controls import ControlPanel
from desk.db import Database
from desk.exchange.dxtrade import DXtradeClient, DXtradeError
from desk.llm import LLMClient
from desk.log_setup import setup_logging
from desk.news import NewsFeed
from desk.notify import LogNotifier
from desk.prop.brokers import BrokerError, DXtradeBroker, PaperPropBroker, PriceSource
from desk.prop.engine import PropEngine
from desk.prop.live_signals import LiveSignals
from desk.prop.review import PropReviewer
from desk.prop.rules import tradeify_1step, tradeify_2step
from desk.prop.status import prop_positions_text, prop_status_text
from desk.system import InstanceLock, keep_awake
from desk.telegram import CommandHandler, MultiNotifier, TelegramCommands

log = logging.getLogger("desk.prop.main")

EXIT_OK, EXIT_REFUSED, EXIT_HUNG = 0, 2, 3
K_STOPPED = "bot_stopped_cleanly_at"
HANG_LIMIT = 600          # seconds without a finished minute -> restart


def build_broker(config, secrets, db, clock, rules):
    p = config.prop
    if p.broker == "paper":
        names = [f"paper-{i + 1}" for i in range(p.paper_accounts)]
        prices = PriceSource(dict(p.price_symbols), httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT}))
        return PaperPropBroker(db, clock, rules, prices, names)
    client = DXtradeClient(secrets.dxtrade_url or "", secrets.dxtrade_username or "", secrets.dxtrade_domain or "default",
                           secrets.dxtrade_password or "")
    codes = [c.strip() for c in (secrets.dxtrade_accounts or "").split(",") if c.strip()]
    broker = DXtradeBroker(client, dict(p.symbols), codes, clock)
    missing = broker.load_instruments()
    if missing:
        raise BrokerError(f"the platform doesn't list these coins: {', '.join(missing)} (remove them from prop.symbols)")
    return broker


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the prop-firm bot")
    ap.add_argument("--config", help="path to a config.yaml")
    ap.add_argument("--once", action="store_true", help="run one minute's checks (and today's rotation if due), then exit")
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return EXIT_REFUSED
    secrets = load_secrets()
    setup_logging(config.path("logs"), process_name="prop", level=config.logging.level,
                  max_mb=config.logging.file_max_mb, backups=config.logging.file_backups,
                  secrets=secrets.secret_values())
    p = config.prop
    clock = Clock(config.tz)
    db = Database(config.path("database"), clock)
    db.migrate()
    lock = InstanceLock(config.path("database").with_suffix(".lock"))
    if not lock.acquire():
        print("Another copy of the bot is already running on this database. Only one may run at a time.")
        return EXIT_REFUSED

    rules = (tradeify_2step if p.firm == "tradeify_2step" else tradeify_1step)(p.account_size)
    http = httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    from desk.main import setup_telegram_alerts           # shared with the CoinSpot bot
    tg, tg_note = setup_telegram_alerts(SimpleNamespace(secrets=secrets))
    notify = MultiNotifier(LogNotifier(db), tg) if tg else MultiNotifier(LogNotifier(db))
    controls = ControlPanel(db, clock)
    llm = LLMClient(config.llm, db, clock, api_key=secrets.anthropic_api_key)
    reviewer = PropReviewer(p.ai_review, llm, db, clock, dict(p.news_aliases))
    news = NewsFeed(config, http, clock, db, cryptopanic_token=secrets.cryptopanic_token)
    try:
        broker = build_broker(config, secrets, db, clock, rules)
        engine = PropEngine(config, rules, broker, LiveSignals(config), db, clock, notify, controls, reviewer,
                            before_rebalance=(news.fetch_all if p.ai_review != "off" else None))
        warnings = engine.startup()
    except (BrokerError, DXtradeError, RuntimeError) as exc:
        print(f"\nREFUSING TO START: {exc}")
        db.log_event("startup_refused", str(exc), level="CRITICAL")
        lock.release()
        return EXIT_REFUSED

    target = "none (funded)" if p.phase == "funded" else f"+{rules.targets_pct[int(p.phase) - 1]:g}%"
    print("=" * 78)
    print(f" Crypto Desk - PROP MODE ({'SIMULATED account' if p.broker == 'paper' else 'REAL DXtrade account'})   "
          f"started {clock.local_now():%Y-%m-%d %H:%M %Z}")
    print("=" * 78)
    print(f"  {rules.name} ${p.account_size:,.0f}, phase {p.phase} (target {target}); "
          f"limits {rules.daily_loss_pct:g}% daily / {rules.max_loss_pct:g}% overall")
    print(f"  Strategy: daily rotation into the {p.strategy.top} strongest of {len(p.symbols)} coins at "
          f"{p.strategy.rebalance_hour_utc:02d}:00 UTC; risk {p.risk.risk_pct:g}% per trade; "
          f"brake at -{p.risk.daily_brake_pct:g}% a day")
    print(f"  Accounts: {', '.join(engine.accounts)}   |   AI review: {p.ai_review}")
    print(f"  Telegram: {tg_note}")
    for w in warnings:
        print(f"  ! {w}")
    print("  Leave this running. Ctrl+C stops it cleanly.")
    print("=" * 78)
    notify.send("startup", f"Prop bot started ({p.broker}, phase {p.phase}, {len(engine.accounts)} account(s)). "
                           + (" ".join(warnings) if warnings else ""))

    if args.once:
        engine.minute()
        print(prop_status_text(db, clock, config, engine.last_snapshots))
        notify.flush()
        lock.release()
        return EXIT_OK

    if tg:
        handler = CommandHandler(controls, lambda: prop_status_text(db, clock, config, engine.last_snapshots),
                                 lambda: prop_positions_text(db, clock, config, engine.last_snapshots), clock.now)
        TelegramCommands(tg.client, tg.chat_id, handler, db=db).start()

    from desk.main import stop_on_sigterm
    stop_on_sigterm()
    awake = config.schedule.keep_pc_awake and keep_awake(True)
    progress = {"t": time.monotonic()}

    def hang_guard():
        while True:
            time.sleep(15)
            if time.monotonic() - progress["t"] > HANG_LIMIT:
                msg = f"The prop loop hasn't finished a check for {HANG_LIMIT // 60} minutes: restarting"
                log.critical(msg)
                notify.send("error", msg)
                notify.flush()
                os._exit(EXIT_HUNG)

    threading.Thread(target=hang_guard, name="hang-guard", daemon=True).start()
    errors = 0
    try:
        while True:
            started = time.monotonic()
            try:
                engine.minute()
                errors = 0
            except Exception as exc:                          # one bad minute must not stop the bot
                errors += 1
                log.exception("prop minute failed (#%d)", errors)
                db.log_event("error", f"prop loop error: {type(exc).__name__}: {exc}", level="ERROR")
                db.kv_set("heartbeat_fast", iso(clock.now()))   # the loop itself is alive
                if errors == 3:
                    notify.send("error", f"The prop bot failed 3 checks in a row: {type(exc).__name__}: {exc}. "
                                         f"Still retrying; see logs/prop.log.")
            progress["t"] = time.monotonic()
            time.sleep(max(1.0, 60 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nStopping...")
        db.kv_set(K_STOPPED, iso(clock.now()))
        db.log_event("shutdown", "Prop bot stopped by user (Ctrl+C or docker stop)")
        notify.send("info", "Prop bot stopped on purpose (Ctrl+C, docker compose stop, or a server restart). "
                            + ("Protective stop orders stay on the platform." if p.broker == "dxtrade" else
                               "Simulated positions aren't watched while it's off."))
        notify.flush()
    finally:
        if awake:
            keep_awake(False)
        lock.release()
        db.close()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run())
