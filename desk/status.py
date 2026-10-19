"""Status report: run with  status.bat  (or  python -m desk.status).

Reads the database only, so it works while the bot is running (in another
window) and never interferes with it. Telegram's /status and /positions use
the same functions.
"""

from __future__ import annotations

import argparse
import sys

from desk.clock import Clock, iso, parse_iso
from desk.config import Config, ConfigError, load_config
from desk.controls import ControlPanel
from desk.db import Database
from desk.fmt import fmt_age, fmt_price, shorten
from desk.portfolio import Portfolio
from desk.strategy import apply_strategy

LINE = "=" * 78


def running_lines(db: Database, clock: Clock, config: Config) -> list[str]:
    now = clock.now()
    out = []
    cycle_every = 86400 if config.strategy.mode == "trend" else config.schedule.agent_cycle_minutes * 60
    label_cycle = "Daily trend check" if config.strategy.mode == "trend" else "Agent cycles"
    for which, label, every in (("fast", "Price checks", config.schedule.fast_check_seconds),
                                ("cycle", label_cycle, cycle_every)):
        v = db.kv_get(f"heartbeat_{which}")
        if not v:
            out.append(f"{label:<18} never run yet")
            continue
        age = (now - parse_iso(v)).total_seconds()
        ok = age <= every * 2 + 60
        out.append(f"{label:<18} last {fmt_age(age)} ago {'(running)' if ok else '<< NOT RUNNING? is paper.bat open?'}")
    return out


def status_text(db: Database, clock: Clock, config: Config, mode: str = "paper", compact: bool = False) -> str:
    pf = Portfolio(db, clock, config, mode)
    now = clock.now()
    lines = [f"Crypto Desk ({mode.upper()}, {config.strategy.mode} strategy)  {clock.local_now():%a %d %b %H:%M %Z}"]
    lines += running_lines(db, clock, config)
    lines.append(f"Switches: {ControlPanel(db, clock).state().describe()}")
    snap = db.query_one("SELECT * FROM equity_snapshots WHERE mode = ? ORDER BY id DESC LIMIT 1", (mode,))
    start = pf.starting_balance()
    if snap:
        eq = snap["equity_aud"]
        lines.append(f"Equity A${eq:,.2f} ({(eq / start - 1) * 100:+.2f}% since start, A${start:,.0f})")
        lines.append(f"Cash A${snap['cash_aud']:,.2f}")
        if snap["day_pnl_pct"] is not None:
            lines.append(f"Today {snap['day_pnl_pct']:+.2f}% (daily loss limit -{config.risk.daily_loss_limit_pct:g}%)")
    else:
        lines.append(f"Equity A${start:,.2f} (no price checks recorded yet)")

    positions = pf.open_positions()
    lines.append(f"Open positions: {len(positions)}" + (f" ({', '.join(p.coin for p in positions)})" if positions else ""))

    since = iso(clock.day_start_utc())
    trades = db.query("SELECT * FROM trades WHERE mode = ? AND ts >= ? ORDER BY id", (mode,since))
    buys = sum(1 for t in trades if t["side"] == "buy")
    sells = [t for t in trades if t["side"] == "sell"]
    realised = sum(t["gain_aud"] or 0 for t in sells)
    lines.append(f"Today: {buys} buy(s), {len(sells)} sell(s), realised A${realised:+,.2f}")
    spent = db.query_one("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM llm_calls WHERE ts >= ?", (since,))
    lines.append(f"AI today: {spent[1]} call(s), US${spent[0]:.4f} of US${config.llm.daily_spend_cap_usd:.2f}")
    if compact:
        return "\n".join(lines)

    lines.append("")
    lines.append("TODAY'S TRADES")
    for t in trades:
        when = parse_iso(t["ts"]).astimezone(clock.tz)
        extra = f"  P&L A${t['gain_aud']:+,.2f} ({t['reason']})" if t["side"] == "sell" else ""
        lines.append(f"  {when:%H:%M} {t['side'].upper():<4} {t['qty']:.8g} {t['coin']} @ {fmt_price(t['price'])} "
                     f"= A${t['aud_amount']:,.2f}{extra}")
    if not trades:
        lines.append("  none yet")
    debates = db.query("SELECT outcome, COUNT(*) n FROM debates WHERE ts >= ? GROUP BY outcome", (since,))
    if debates:
        lines.append("  Debates: " + ", ".join(f"{r['n']} {r['outcome']}" for r in debates))
    vetoes = db.query_one("SELECT COUNT(*) FROM risk_decisions WHERE ts >= ? AND approved = 0 AND kind='entry'",
                          (since,))[0]
    lines.append(f"  Risk Manager vetoes: {vetoes}")
    lines.append("")
    lines.append("RECENT EVENTS")
    for e in db.query("SELECT * FROM events WHERE kind NOT IN ('cycle', 'check', 'telegram') ORDER BY id DESC LIMIT 8"):
        when = parse_iso(e["ts"]).astimezone(clock.tz)
        lines.append(f"  {when:%d %b %H:%M} {e['level']:<7} {shorten(e['message'], 90)}")
    last_cycle = db.query_one("SELECT * FROM events WHERE kind = 'cycle' ORDER BY id DESC LIMIT 1")
    if last_cycle:
        lines.append("")
        lines.append(f"Last cycle: {shorten(last_cycle['message'], 160)}")
    return "\n".join(lines)


def positions_text(db: Database, clock: Clock, config: Config, quotes: dict | None, mode: str = "paper") -> str:
    """Open positions with live P&L (quotes: CoinSpot prices, or None if unavailable)."""
    pf = Portfolio(db, clock, config, mode)
    positions = pf.open_positions()
    if not positions:
        return "No open positions."
    now = clock.now()
    lines = []
    for p in positions:
        q = (quotes or {}).get(p.coin)
        head = f"{p.coin}: {p.qty:.8g} bought @ A${p.entry_price:,.6g} ({fmt_age((now - p.opened_at).total_seconds())} ago)"
        if q:
            pnl = (q.bid / p.entry_price - 1) * 100
            head += f"\n  now A${q.bid:,.6g} ({pnl:+.2f}%, A${(q.bid - p.entry_price) * p.qty:+,.2f} before fees)"
        stop = p.stop_loss
        if p.trailing_active:
            dist = p.trail_pct if p.trail_pct is not None else config.exits.trailing_distance_pct
            stop = max(stop, p.highest_price * (1 - dist / 100))
            head += f"\n  trailing stop A${stop:,.6g} ({dist:g}% under the high of A${p.highest_price:,.6g})"
        else:
            head += f"\n  stop A${stop:,.6g}"
        if p.take_profit < p.entry_price * 5:
            head += f" | target A${p.take_profit:,.6g}"
        lines.append(head)
    if quotes is None:
        lines.append("(live prices unavailable right now)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Show the bot's status (read-only)")
    p.add_argument("--config", help="path to a config.yaml")
    args = p.parse_args(argv)
    try:
        config = apply_strategy(load_config(args.config))
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2
    clock = Clock(config.tz)
    db = Database(config.path("database"), clock)
    db.migrate()
    try:
        if config.prop.enabled:
            from desk.prop.status import prop_positions_text, prop_status_text
            print(LINE)
            print(prop_status_text(db, clock, config))
            print("\nOPEN POSITIONS")
            print("  " + prop_positions_text(db, clock, config).replace("\n", "\n  "))
            print(LINE)
            return 0
        print(LINE)
        print(status_text(db, clock, config))
        print("\nOPEN POSITIONS")
        print("  " + positions_text(db, clock, config, None).replace("\n", "\n  ").replace(
            "(live prices unavailable right now)", "(run scan.bat for live prices)"))
        print(LINE)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
