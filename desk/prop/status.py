"""Status text for prop mode (status.bat, Telegram /status and /positions)."""

from __future__ import annotations

from datetime import timedelta

from desk.clock import Clock, iso, parse_iso
from desk.config import Config
from desk.controls import ControlPanel
from desk.db import Database
from desk.fmt import fmt_age
from desk.prop.rules import tradeify_1step, tradeify_2step


def _rules(config: Config):
    p = config.prop
    return (tradeify_2step if p.firm == "tradeify_2step" else tradeify_1step)(p.account_size)


def prop_status_text(db: Database, clock: Clock, config: Config, snapshots: dict | None = None) -> str:
    p = config.prop
    rules = _rules(config)
    now = clock.now()
    target = None if p.phase == "funded" else rules.targets_pct[int(p.phase) - 1]
    lines = [f"Crypto Desk PROP ({p.broker.upper()}, {rules.name}, phase {p.phase})  {clock.local_now():%a %d %b %H:%M %Z}"]
    hb = db.kv_get("heartbeat_fast")
    if hb:
        age = (now - parse_iso(hb)).total_seconds()
        lines.append(f"Checks: last {fmt_age(age)} ago ({'running' if age < 300 else 'NOT RUNNING?'})")
    else:
        lines.append("Checks: never run yet")
    last_rot = db.query_one("SELECT ts, message FROM events WHERE kind = 'prop_rotation' ORDER BY id DESC LIMIT 1")
    if last_rot:
        lines.append(f"Last rotation {fmt_age((now - parse_iso(last_rot['ts'])).total_seconds())} ago: {last_rot['message']}")
    lines.append(f"Switches: {ControlPanel(db, clock).state().describe()}")
    for row in db.query("SELECT * FROM prop_accounts WHERE broker = ? ORDER BY account", (p.broker,)):
        a = row["account"]
        snap = (snapshots or {}).get(a)
        if snap is not None:
            bal, eq = snap.balance, snap.equity
        else:
            e = db.query_one("SELECT balance, equity, ts FROM prop_equity WHERE account = ? ORDER BY id DESC LIMIT 1", (a,))
            bal, eq = (e["balance"], e["equity"]) if e else (row["size"], row["size"])
        size = row["size"]
        lines.append("")
        lines.append(f"[{a}] equity ${eq:,.2f} ({eq - size:+,.2f}, {(eq / size - 1) * 100:+.2f}%), balance ${bal:,.2f}")
        if row["day_ref"] is not None:
            lines.append(f"  today {(eq - row['day_ref']) / size * 100:+.2f}% (brake -{p.risk.daily_brake_pct:g}%, "
                         f"firm limit -{rules.daily_loss_pct:g}%)")
        floor = size * (1 - rules.max_loss_pct / 100)
        lines.append(f"  cushion to the -{rules.max_loss_pct:g}% floor (${floor:,.0f}): ${eq - floor:,.2f}")
        if row["target_hit_at"]:
            lines.append(f"  PHASE TARGET REACHED {row['target_hit_at'][:10]}: not trading")
        elif target is not None:
            need = size * (1 + target / 100) - eq
            lines.append(f"  target +{target:g}% (${size * (1 + target / 100):,.0f}): ${max(0.0, need):,.2f} to go")
        if row["halted_until"] and parse_iso(row["halted_until"]) > now:
            lines.append(f"  resting after the daily brake until {row['halted_until'][11:16]} UTC")
        n_open = db.query_one("SELECT COUNT(*) FROM prop_positions WHERE account = ?", (a,))[0]
        since = iso(now - timedelta(days=7))
        week = db.query_one("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM prop_trades WHERE account = ? AND closed_at >= ?",
                            (a, since))
        lines.append(f"  {n_open} open | last 7 days: {week[0]} closed trade(s), ${week[1]:+,.0f}")
    rv = db.query_one("SELECT COUNT(*), SUM(verdict = 'veto') FROM prop_reviews WHERE ts >= ?",
                      (iso(now - timedelta(days=30)),))
    if rv and rv[0]:
        lines.append(f"\nAI reviews (30 days, {p.ai_review} mode): {rv[0]} buys reviewed, {rv[1] or 0} vetoes")
    return "\n".join(lines)


def prop_positions_text(db: Database, clock: Clock, config: Config, snapshots: dict | None = None) -> str:
    rows = db.query("SELECT pp.* FROM prop_positions pp JOIN prop_accounts pa ON pa.account = pp.account "
                    "WHERE pa.broker = ? ORDER BY pp.account, pp.coin", (config.prop.broker,))
    if not rows:
        return "No open prop positions."
    now = clock.now()
    out = []
    for r in rows:
        snap = (snapshots or {}).get(r["account"])
        mark = snap.positions[r["coin"]].mark_price if snap and r["coin"] in snap.positions else None
        line = (f"[{r['account']}] {r['coin']}: {r['qty']:g} @ {r['entry_price']:,.6g} "
                f"({fmt_age((now - parse_iso(r['opened_at'])).total_seconds())} ago), stop {r['stop']:,.6g}")
        if mark:
            line += f", now {mark:,.6g} (${r['side'] * r['qty'] * (mark - r['entry_price']):+,.0f})"
        out.append(line)
    return "\n".join(out)
