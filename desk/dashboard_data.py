"""Read-only queries behind the dashboard (kept separate so they can be tested)."""

from __future__ import annotations

import json

import pandas as pd

from desk.clock import Clock, iso, parse_iso
from desk.config import Config
from desk.db import Database
from desk.portfolio import Portfolio


def _local(ts: str, clock: Clock) -> str:
    return parse_iso(ts).astimezone(clock.tz).strftime("%d %b %H:%M")


def equity_series(db: Database, mode: str = "paper") -> pd.Series:
    rows = db.query("SELECT ts, equity_aud FROM equity_snapshots WHERE mode = ? ORDER BY id", (mode,))
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([r["equity_aud"] for r in rows], index=pd.DatetimeIndex([parse_iso(r["ts"]) for r in rows]))
    return s[~s.index.duplicated(keep="last")]


def kpis(db: Database, clock: Clock, config: Config, mode: str = "paper") -> dict:
    pf = Portfolio(db, clock, config, mode)
    snap = db.query_one("SELECT * FROM equity_snapshots WHERE mode = ? ORDER BY id DESC LIMIT 1", (mode,))
    start = pf.starting_balance()
    since = iso(clock.day_start_utc())
    trades_today = db.query_one("SELECT COUNT(*) FROM trades WHERE mode = ? AND ts >= ?", (mode, since))[0]
    ai = db.query_one("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ts >= ?", (since,))
    return {
        "equity": snap["equity_aud"] if snap else start,
        "start": start,
        "total_pct": ((snap["equity_aud"] if snap else start) / start - 1) * 100,
        "today_pct": snap["day_pnl_pct"] if snap and snap["day_pnl_pct"] is not None else 0.0,
        "cash": snap["cash_aud"] if snap else start,
        "open_positions": len(pf.open_positions()),
        "trades_today": trades_today,
        "ai_calls_today": ai[0],
        "ai_cost_today": ai[1],
    }


def positions_df(db: Database, clock: Clock, config: Config, quotes: dict | None, mode: str = "paper") -> pd.DataFrame:
    pf = Portfolio(db, clock, config, mode)
    now = clock.now()
    recs = []
    for p in pf.open_positions():
        q = (quotes or {}).get(p.coin)
        dist = p.trail_pct if p.trail_pct is not None else config.exits.trailing_distance_pct
        stop = max(p.stop_loss, p.highest_price * (1 - dist / 100)) if p.trailing_active else p.stop_loss
        recs.append({
            "Coin": p.coin, "Quantity": p.qty, "Bought at (A$)": p.entry_price,
            "Now (A$)": q.bid if q else None,
            "P&L %": (q.bid / p.entry_price - 1) * 100 if q else None,
            "P&L A$": (q.bid - p.entry_price) * p.qty - p.entry_fee_aud if q else None,
            "Stop (A$)": stop, "Trailing": "on" if p.trailing_active else "not yet",
            "Target (A$)": p.take_profit if p.take_profit < p.entry_price * 5 else None,
            "Held": f"{(now - p.opened_at).total_seconds() / 3600:.1f} h",
        })
    return pd.DataFrame(recs)


def trades_df(db: Database, clock: Clock, mode: str = "paper", limit: int = 100) -> pd.DataFrame:
    rows = db.query("SELECT * FROM trades WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))
    return pd.DataFrame([{
        "When": _local(r["ts"], clock), "Side": r["side"].upper(), "Coin": r["coin"], "Quantity": r["qty"],
        "Price (A$)": r["price"], "Amount (A$)": r["aud_amount"], "Fee (A$)": r["fee_aud"],
        "Reason": r["reason"], "P&L (A$)": r["gain_aud"],
    } for r in rows])


def decisions_df(db: Database, clock: Clock, days: int = 1) -> pd.DataFrame:
    since = iso(clock.now() - pd.Timedelta(days=days))
    rows = db.query("SELECT * FROM risk_decisions WHERE ts >= ? ORDER BY id DESC", (since,))
    return pd.DataFrame([{
        "When": _local(r["ts"], clock), "Type": r["kind"], "Coin": r["coin"],
        "Risk Manager": "APPROVED" if r["approved"] else "REJECTED",
        "Why not": "; ".join(json.loads(r["failures"])) or "-",
        "Amount (A$)": r["notional_aud"] or None,
    } for r in rows])


def debates(db: Database, clock: Clock, days: int = 1, limit: int = 30) -> list[dict]:
    since = iso(clock.now() - pd.Timedelta(days=days))
    out = []
    for r in db.query("SELECT * FROM debates WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit)):
        out.append({
            "when": _local(r["ts"], clock), "kind": r["kind"], "coin": r["coin"], "outcome": r["outcome"],
            "proposal": json.loads(r["proposal"]) if r["proposal"] else None,
            "verdict": json.loads(r["verdict"]) if r["verdict"] else None,
            "problems": json.loads(r["problems"]),
        })
    return out


def events_df(db: Database, clock: Clock, limit: int = 60) -> pd.DataFrame:
    rows = db.query("SELECT * FROM events WHERE kind NOT IN ('check') ORDER BY id DESC LIMIT ?", (limit,))
    return pd.DataFrame([{"When": _local(r["ts"], clock), "Level": r["level"], "Type": r["kind"],
                          "Message": r["message"]} for r in rows])


def ai_by_agent(db: Database, clock: Clock) -> pd.DataFrame:
    rows = db.query("SELECT agent, COUNT(*) n, COALESCE(SUM(cost_usd), 0) cost, SUM(1 - ok) failed "
                    "FROM llm_calls WHERE ts >= ? GROUP BY agent ORDER BY cost DESC", (iso(clock.day_start_utc()),))
    return pd.DataFrame([{"Agent": r["agent"], "Calls": r["n"], "Cost (US$)": round(r["cost"], 5),
                          "Failed": r["failed"]} for r in rows])


def active_blocks(db: Database, clock: Clock) -> pd.DataFrame:
    rows = db.query("SELECT * FROM news_events WHERE block_until > ? ORDER BY ts DESC", (iso(clock.now()),))
    return pd.DataFrame([{"Coin": r["coin"], "Why": r["summary"], "Until": _local(r["block_until"], clock)}
                         for r in rows])
