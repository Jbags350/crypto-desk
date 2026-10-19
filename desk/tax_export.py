"""Australian tax CSV: export_tax.bat --fy 2026   (or  python -m desk.tax_export --fy 2026)

One row per trade. Every SELL is a CGT event (a disposal); every BUY sets the
cost base for the coins it bought. The bot matches each sale to the exact
buy it came from (it tracks every position separately), and the cost base
includes the buy fee while the proceeds are net of the sell fee.

FY2026 = 1 July 2025 to 30 June 2026, using your local time zone (config.yaml).
LIVE trades only by default. --include-paper adds paper trades (for trying it out).

This is a record-keeping aid, not tax advice. Give the file to your accountant.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, time
from pathlib import Path

from desk.clock import Clock, iso, parse_iso
from desk.config import ConfigError, load_config
from desk.db import Database

COLUMNS = ["date_local", "date_utc", "mode", "coin", "side", "quantity", "price_aud", "aud_amount", "fee_aud",
           "net_aud", "cost_base_aud", "capital_gain_aud", "days_held", "acquired_date_local", "reason",
           "order_id", "position_id"]


def fy_bounds(fy: int, tz) -> tuple[datetime, datetime]:
    """Australian financial year `fy` (e.g. 2026) = 1 Jul (fy-1) 00:00 to 1 Jul fy 00:00, local time."""
    start = datetime.combine(date(fy - 1, 7, 1), time(0, 0), tzinfo=tz)
    end = datetime.combine(date(fy, 7, 1), time(0, 0), tzinfo=tz)
    return start, end


def export(db: Database, tz, fy: int, out: Path, include_paper: bool = False) -> dict:
    start, end = fy_bounds(fy, tz)
    modes = ("live", "paper") if include_paper else ("live",)
    rows = db.query(
        f"SELECT * FROM trades WHERE ts >= ? AND ts < ? AND mode IN ({','.join('?' * len(modes))}) ORDER BY ts, id",
        (iso(start), iso(end), *modes))
    totals = {"trades": 0, "buys": 0, "sells": 0, "gains": 0.0, "losses": 0.0, "fees": 0.0}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows:
            ts = parse_iso(r["ts"])
            sell = r["side"] == "sell"
            net = r["aud_amount"] - r["fee_aud"] if sell else r["aud_amount"] + r["fee_aud"]
            acquired = parse_iso(r["acquired_at"]) if r["acquired_at"] else None
            days = (ts - acquired).days if (sell and acquired) else ""
            w.writerow([
                ts.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"), ts.strftime("%Y-%m-%d %H:%M:%S"), r["mode"],
                r["coin"], r["side"].upper(), f"{r['qty']:.8f}", f"{r['price']:.8f}", f"{r['aud_amount']:.2f}",
                f"{r['fee_aud']:.2f}", f"{net:.2f}",
                f"{r['cost_base_aud']:.2f}" if sell and r["cost_base_aud"] is not None else "",
                f"{r['gain_aud']:.2f}" if sell and r["gain_aud"] is not None else "",
                days, acquired.astimezone(tz).strftime("%Y-%m-%d") if (sell and acquired) else "",
                r["reason"] or "", r["order_id"], r["position_id"],
            ])
            totals["trades"] += 1
            totals["fees"] += r["fee_aud"]
            if sell:
                totals["sells"] += 1
                g = r["gain_aud"] or 0.0
                totals["gains" if g >= 0 else "losses"] += g
            else:
                totals["buys"] += 1
    return totals


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Export trades for Australian tax (one row per trade)")
    p.add_argument("--fy", type=int, help="financial year ending 30 June, e.g. 2026 (default: the current one)")
    p.add_argument("--include-paper", action="store_true", help="include paper (fake money) trades")
    p.add_argument("--out", help="output file (default: reports/tax_FY<year>.csv)")
    p.add_argument("--config", help="path to a config.yaml")
    args = p.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2
    clock = Clock(config.tz)
    today = clock.local_date()
    fy = args.fy or (today.year + 1 if today.month >= 7 else today.year)
    db = Database(config.path("database"), clock)
    db.migrate()
    suffix = "_incl_paper" if args.include_paper else ""
    out = Path(args.out) if args.out else config.path("reports") / f"tax_FY{fy}{suffix}.csv"
    try:
        t = export(db, config.tz, fy, out, include_paper=args.include_paper)
    finally:
        db.close()
    start, end = fy_bounds(fy, config.tz)
    print(f"Financial year {fy}: {start:%d %b %Y} to {(end.date().replace(day=30, month=6))!s}")
    print(f"Wrote {t['trades']} trade(s) ({t['buys']} buys, {t['sells']} sells) to {out}")
    print(f"Capital gains A${t['gains']:,.2f} | capital losses A${-t['losses']:,.2f} | "
          f"net A${t['gains'] + t['losses']:,.2f} | fees A${t['fees']:,.2f}")
    if not args.include_paper:
        print("(Live trades only. Add --include-paper to include paper trades.)")
    print("Not tax advice: give this file to your accountant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
