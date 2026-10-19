"""Run a backtest: backtest.bat  (or  python -m desk.backtest)

  backtest.bat                                   settings from config.yaml, last backtest.days days
  backtest.bat --days 365
  backtest.bat --start 2026-01-01 --end 2026-07-01
  backtest.bat --mode trend --days 1825          daily trend-following over 5 years
  backtest.bat --mode swing --entry limit --tp limit
  backtest.bat --coins BTC,ETH --spread 0.1
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from datetime import UTC, datetime, timedelta

import httpx

from desk import __version__
from desk.backtest.engine import Backtester
from desk.backtest.history import get_history
from desk.backtest.report import write_report
from desk.config import ConfigError, load_config
from desk.data.candles import CandleError

# history needed before the test starts so the slowest indicator is ready
WARMUP = {"day": timedelta(days=12), "swing": timedelta(days=40), "trend": timedelta(days=90)}
BASE_TF = {"day": "5m", "swing": "5m", "trend": "1h"}


def _floor(t: datetime, minutes: int) -> datetime:
    t = t.replace(second=0, microsecond=0)
    return t - timedelta(minutes=t.minute % minutes, hours=0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backtest the desk's rules + Risk Manager + exits on past prices")
    p.add_argument("--days", type=int, help="how many days to test (default: backtest.days in config.yaml)")
    p.add_argument("--start", help="first day, YYYY-MM-DD (instead of --days)")
    p.add_argument("--end", help="day after the last day, YYYY-MM-DD (default: now)")
    p.add_argument("--coins", help="e.g. BTC,ETH (default: all coins in config.yaml)")
    p.add_argument("--mode", choices=["day", "swing", "trend"], help="strategy (default: strategy.mode)")
    p.add_argument("--entry", choices=["market", "limit"], help="entry order type (default: execution.entry_order)")
    p.add_argument("--tp", choices=["market", "limit"], help="take-profit order type")
    p.add_argument("--spread", type=float, help="assumed CoinSpot spread in %% (default from config)")
    p.add_argument("--offline", action="store_true", help="use only already-downloaded prices")
    p.add_argument("--open", action="store_true", help="open the HTML report when done")
    p.add_argument("--config", help="path to a config.yaml")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2
    if args.mode:
        config = config.model_copy(update={"strategy": config.strategy.model_copy(update={"mode": args.mode})})
    ex_upd = {k: v for k, v in (("entry_order", args.entry), ("take_profit_order", args.tp)) if v}
    if ex_upd:
        config = config.model_copy(update={"execution": config.execution.model_copy(update=ex_upd)})
    mode = config.strategy.mode
    base_tf = BASE_TF[mode]

    now = datetime.now(UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else _floor(now, 60 if base_tf == "1h" else 5)
    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    else:
        start = end - timedelta(days=args.days or config.backtest.days)
    if start >= end:
        print("The start must be before the end.")
        return 2
    try:
        coins = [config.coin(c.strip()) for c in args.coins.split(",")] if args.coins else list(config.coins)
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 2
    if "BTC" not in [c.code for c in config.coins]:
        print("BTC must be in the coins list in config.yaml (it's the benchmark).")
        return 2
    needed = list(dict.fromkeys([c.code for c in coins] + ["BTC"]))     # BTC is always loaded for "just hold BTC"

    print("=" * 78)
    print(f" Crypto Desk backtest   {start:%Y-%m-%d} to {end:%Y-%m-%d}   coins: {', '.join(c.code for c in coins)}")
    print(f" strategy: {mode}   entry orders: {config.execution.entry_order}   "
          f"take-profit orders: {config.execution.take_profit_order}   candles: {base_tf}")
    print("=" * 78)
    cache = config.path("candle_cache")

    def dl_progress(code, cursor, s, e):
        done = (cursor - s) / (e - s) if e > s else 1
        print(f"\r  downloading {code} price history: {min(done, 1):.0%}   ", end="", flush=True)

    candles = {}
    warm = WARMUP[mode]
    with httpx.Client(headers={"User-Agent": f"crypto-desk/{__version__}"}) as http:
        for code in needed:
            try:
                df = get_history(http, config.coin(code), start - warm, end, cache, now,
                                 offline=args.offline, progress=dl_progress, tf=base_tf)
            except CandleError as exc:
                print(f"\nCouldn't get price history: {exc}")
                return 1
            print(f"\r  {code}: {len(df):,} {base_tf} candles ready{' ' * 30}")
            if df.empty:
                print(f"  ! {code}: no price history for this period")
                return 1
            if df.index[0] > start - warm + timedelta(days=2):
                print(f"  ! {code}: history only starts {df.index[0]:%Y-%m-%d}; its early signals need warm-up")
            candles[code] = df

    def run_progress(i, n):
        print(f"\r  replaying: {i / n:.0%}   ", end="", flush=True)

    bt = Backtester(config, candles, coins, start, end, base_tf=base_tf, spread_pct=args.spread)
    result = bt.run(progress=run_progress)
    print()
    out_dir = config.path("reports") / f"backtest_{mode}_{datetime.now():%Y%m%d_%H%M%S}"
    m = write_report(result, out_dir)
    print()
    print((out_dir / "summary.txt").read_text(encoding="utf-8"))
    print(f"Report: {out_dir / 'report.html'}")
    print("(The AI agents and news are not part of a backtest; see the report's notes.)")
    if args.open:
        try:
            os.startfile(out_dir / "report.html")          # Windows
        except AttributeError:
            webbrowser.open((out_dir / "report.html").as_uri())
    return 0 if m else 1


if __name__ == "__main__":
    sys.exit(main())
