"""System check: run with  check.bat  (or  python -m desk.check).

Shows live CoinSpot prices and spreads, compares them with the global price,
checks candle data is fresh, and tests whichever API keys are in .env.
Read-only: it never places orders.
"""

from __future__ import annotations

import argparse
import sys

from desk.bootstrap import App, build_app
from desk.config import ConfigError
from desk.data.candles import CandleError
from desk.data.market import divergence_pct, to_aud
from desk.exchange.coinspot import CoinSpotAuthError, CoinSpotError
from desk.fmt import fmt_age, fmt_price

LINE = "=" * 66


def run(app: App) -> int:
    cfg, clock = app.config, app.clock
    problems: list[str] = []
    warnings: list[str] = list(app.secrets.problems)

    print(LINE)
    print(f" Crypto Desk - system check        {clock.local_now():%Y-%m-%d %H:%M %Z}")
    print(LINE)
    mode = "LIVE (real money)" if app.secrets.live_trading else "PAPER (fake money)"
    print(f"Mode ............ {mode}")
    print(f"Config .......... OK ({len(cfg.coins)} coins: {', '.join(c.code for c in cfg.coins)})")
    print(f"Database ........ OK {cfg.paths.database} (schema v{app.db.schema_version()})")

    # ---- CoinSpot prices --------------------------------------------
    print("\nCoinSpot prices (AUD)")
    snap = None
    try:
        snap = app.market.snapshot()
    except CoinSpotError as exc:
        problems.append(f"CoinSpot prices unavailable: {exc.message}")
        print(f"  FAILED: {exc.message}")
    if snap:
        print(f"  {'Coin':<6}{'Bid':>14}{'Ask':>14}{'Spread':>9}{'Last':>14}")
        for c in cfg.coins:
            q = snap.quotes.get(c.code)
            if q is None:
                problems.append(f"CoinSpot has no price for {c.code}; check the coin code in config.yaml")
                print(f"  {c.code:<6}{'(no price on CoinSpot)':>51}")
                continue
            print(f"  {c.code:<6}{fmt_price(q.bid):>14}{fmt_price(q.ask):>14}{q.spread_pct:>8.3f}%{fmt_price(q.last):>14}")
        if snap.usdt_aud:
            print(f"  USDT/AUD rate: {snap.usdt_aud.mid:.4f}")
        else:
            warnings.append("CoinSpot didn't return a USDT price, so Binance prices can't be converted to AUD")
        app.db.insert_price_snapshots(
            [(q.coin, q.bid, q.ask, q.last) for q in snap.quotes.values()], source="coinspot")

    # ---- Global price comparison ------------------------------------
    print("\nCoinSpot vs global price")
    print(f"  {'Coin':<6}{'Global (AUD)':>15}{'CoinSpot mid':>15}{'Diff':>9}  Source   Status")
    limit = cfg.data.max_price_divergence_pct
    for c in cfg.coins:
        q = snap.quotes.get(c.code) if snap else None
        try:
            price, quote_ccy, provider = app.candles.spot_price(c)
        except CandleError as exc:
            problems.append(f"No global price for {c.code}: {exc}")
            print(f"  {c.code:<6} FAILED: {exc}")
            continue
        g_aud = to_aud(price, quote_ccy, snap.usdt_aud if snap else None)
        if q is None or g_aud is None:
            print(f"  {c.code:<6}{'?':>15}{'?':>15}{'?':>9}  {provider:<8} can't compare")
            continue
        diff = divergence_pct(q.mid, g_aud)
        ok = abs(diff) <= limit
        if not ok:
            problems.append(f"{c.code}: CoinSpot is {diff:+.2f}% from the global price (limit {limit}%)")
        print(f"  {c.code:<6}{fmt_price(g_aud):>15}{fmt_price(q.mid):>15}{diff:>+8.2f}%  {provider:<8} "
              f"{'OK' if ok else 'TOO FAR APART'}")

    # ---- Candles ----------------------------------------------------
    print("\nCandles (used for RSI / MACD / EMA / ATR)")
    print(f"  {'Coin':<6}{'TF':<5}{'Source':<9}{'Count':>6}  {'Newest closed':<17}{'Age':>9}  Status")
    now = clock.now()
    for c in cfg.coins:
        for tf in cfg.data.timeframes:
            try:
                cs = app.candles.get(c, tf, cfg.data.candles_per_timeframe)
            except CandleError as exc:
                problems.append(str(exc))
                print(f"  {c.code:<6}{tf:<5} FAILED: {exc}")
                continue
            fresh = cs.is_fresh(now, cfg.data.max_candle_lag_multiple)
            if not fresh:
                problems.append(f"{c.code} {tf} candles are stale (newest closed {fmt_age(cs.lag_seconds(now))} ago)")
            if cs.provider != cfg.data.candle_providers[0]:
                warnings.append(f"{c.code} {tf}: using fallback provider {cs.provider}")
            closed_local = cs.last_close_time.astimezone(cfg.tz)
            print(f"  {c.code:<6}{tf:<5}{cs.provider:<9}{len(cs.df):>6}  {closed_local:%a %H:%M %Z}"
                  f"{fmt_age(cs.lag_seconds(now)):>11}  {'OK' if fresh else 'STALE'}")

    # ---- Keys -------------------------------------------------------
    print("\nAPI keys")
    s = app.secrets
    if s.has_readonly_key or s.has_full_key:
        label = "read-only key" if s.has_readonly_key else "full key (no read-only key set)"
        try:
            app.coinspot.readonly_status()
            n = len(app.coinspot.balances())
            print(f"  CoinSpot {label} ... OK (CoinSpot accepted it; {n} balance entries visible)")
        except CoinSpotAuthError as exc:
            problems.append(f"CoinSpot {label} rejected: {exc.message}")
            print(f"  CoinSpot {label} ... REJECTED: {exc.message}")
        except CoinSpotError as exc:
            problems.append(f"CoinSpot {label} check failed: {exc.message}")
            print(f"  CoinSpot {label} ... FAILED: {exc.message}")
    else:
        print("  CoinSpot read-only key ... not set (optional for paper trading)")

    if s.has_full_key:
        try:
            app.coinspot.full_status()
            print("  CoinSpot full-access key . OK (CoinSpot accepted it)")
            probe = app.coinspot.withdrawal_probe()
            if probe.enabled is True:
                problems.append("DANGER: the full-access CoinSpot key can WITHDRAW funds. Delete it in CoinSpot "
                                "and create a new one with withdrawals switched off.")
                print(f"  Withdrawal check ......... DANGER: {probe.detail}")
            elif probe.enabled is False:
                print(f"  Withdrawal check ......... OK: {probe.detail}")
            else:
                warnings.append(f"Couldn't confirm withdrawals are off: {probe.detail}")
                print(f"  Withdrawal check ......... UNKNOWN: {probe.detail}")
        except CoinSpotError as exc:
            problems.append(f"CoinSpot full-access key rejected: {exc.message}")
            print(f"  CoinSpot full-access key . REJECTED: {exc.message}")
    else:
        print("  CoinSpot full-access key . not set (only needed for live trading)")
        if s.live_trading:
            problems.append("LIVE_TRADING=true but there is no full-access CoinSpot key")

    print(f"  Claude (Anthropic) key ... {'set' if s.anthropic_api_key else 'not set'} (needed for the AI agents)")
    if not s.telegram_bot_token:
        tg = "not set"
    elif not s.telegram_chat_id:
        tg = "token set, chat ID missing: run telegram_setup.bat"
    else:
        tg = "set"
    print(f"  Telegram ................. {tg} (alerts + phone commands)")
    print(f"  CryptoPanic .............. {'set' if s.cryptopanic_token else 'not set'} (optional)")

    # ---- Result -----------------------------------------------------
    print()
    for w in warnings:
        print(f"  note: {w}")
    if problems:
        print(f"\nRESULT: {len(problems)} PROBLEM(S) FOUND")
        for p in problems:
            print(f"  - {p}")
    else:
        print("\nRESULT: ALL CHECKS PASSED")
    print(LINE)
    app.db.log_event("check", "system check " + ("failed" if problems else "passed"),
                     level="WARNING" if problems else "INFO", data={"problems": problems, "warnings": warnings})
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crypto Desk system check (read-only)")
    parser.add_argument("--config", help="path to a config.yaml (default: config/config.yaml)")
    args = parser.parse_args(argv)
    try:
        app = build_app("check", config_path=args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2
    try:
        return run(app)
    finally:
        app.close()


if __name__ == "__main__":
    sys.exit(main())
