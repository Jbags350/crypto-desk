"""Risk Manager demo: run with  risk_demo.bat  (or  python -m desk.risk_demo).

Shows the Risk Manager judging sample trade ideas against LIVE CoinSpot
prices and your config.yaml limits. No AI, no orders, nothing saved as a trade.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from desk.bootstrap import App, build_app
from desk.config import ConfigError
from desk.exchange.coinspot import CoinSpotError
from desk.fmt import fmt_price
from desk.models import Position
from desk.risk import Controls, EntryRequest, RiskDecision, RiskManager
from desk.risk_inputs import fresh_paper_portfolio, market_state

LINE = "=" * 78


def show(label: str, d: RiskDecision) -> None:
    print(f"\n  {label}")
    if d.approved:
        pct = d.metrics.get("position_pct_of_equity", 0)
        print(f"     => APPROVED: buy {d.qty:.8g} {d.coin} = A${d.notional_aud:,.2f} + fee A${d.fee_aud:,.2f} "
              f"({pct:.2f}% of the portfolio, limited by {d.metrics.get('size_limited_by')})")
    else:
        print("     => REJECTED:")
        for c in d.failures:
            print(f"          x {c.name}: {c.detail}")


def run(app: App, coin: str) -> int:
    cfg, clock = app.config, app.clock
    rm = RiskManager(cfg)
    try:
        snap = app.market.snapshot()
    except CoinSpotError as exc:
        print(f"CoinSpot prices unavailable: {exc.message}")
        return 1
    scan = app.scanner.scan_coin(cfg.coin(coin), snap)
    if scan.ask is None:
        print(f"No CoinSpot price for {coin}.")
        return 1
    mkt = market_state(scan, clock.now(), app.coinspot.health.consecutive_errors)
    pf = fresh_paper_portfolio(cfg)
    ask = scan.ask
    spread = (scan.ask - scan.bid) / ((scan.ask + scan.bid) / 2) * 100
    cost = rm.round_trip_cost_pct(spread)
    need = cfg.risk.min_edge_multiple * cost

    print(LINE)
    print(f" Risk Manager demo        {clock.local_now():%Y-%m-%d %H:%M %Z}")
    print(" No AI, no orders. Sample trade ideas checked against LIVE prices and your config.yaml.")
    print(LINE)
    print(f"\nPretend portfolio: paper A${pf.equity_aud:,.0f}, nothing open, nothing traded today.")
    print(f"{coin} on CoinSpot: bid {fmt_price(scan.bid)}, ask {fmt_price(ask)}, spread {spread:.3f}%")
    print(f"Round-trip cost: fee {cfg.fees.market_order_pct}% x2 + spread {spread:.3f}% + slippage "
          f"{cfg.fees.slippage_estimate_pct}% x2 = {cost:.3f}%")
    print(f"So a take-profit must be at least {cfg.risk.min_edge_multiple:g} x {cost:.3f}% = +{need:.2f}% "
          f"(risk.min_edge_multiple)")
    if scan.problems:
        print(f"Data problems right now (these alone block new trades): {'; '.join(scan.problems)}")

    stop_pct = 1.0
    target_pct = max(2.0, need * 1.25)

    def idea(stop_pct=stop_pct, target_pct=target_pct, conf=65, vote="BUY", entry=ask):
        return EntryRequest(coin, entry, ask * (1 - stop_pct / 100), ask * (1 + target_pct / 100), conf, vote)

    good = idea()
    ctl = Controls()
    print("\nSAMPLE TRADE IDEAS")
    show(f"1. Sensible idea: stop -{stop_pct:.1f}%, target +{target_pct:.2f}%, confidence 65, Skeptic BUY",
         rm.check_entry(good, pf, mkt, ctl))
    show(f"2. Target too small: target +{need * 0.6:.2f}% (doesn't clearly beat costs)",
         rm.check_entry(idea(target_pct=need * 0.6, stop_pct=need * 0.3), pf, mkt, ctl))
    show("3. No stop-loss (stop at 0)",
         rm.check_entry(replace(good, stop_loss_aud=0.0), pf, mkt, ctl))
    show(f"4. Stop too far away: -{cfg.risk.max_stop_distance_pct + 2:.0f}%",
         rm.check_entry(idea(stop_pct=cfg.risk.max_stop_distance_pct + 2, target_pct=20), pf, mkt, ctl))
    show(f"5. Low confidence ({cfg.risk.min_confidence - 10})",
         rm.check_entry(idea(conf=cfg.risk.min_confidence - 10), pf, mkt, ctl))
    show("6. Skeptic said WAIT", rm.check_entry(idea(vote="WAIT"), pf, mkt, ctl))

    print("\nTHE SENSIBLE IDEA (#1) UNDER DIFFERENT DESK CONDITIONS")
    limit = cfg.risk.daily_loss_limit_pct
    down = replace(pf, equity_aud=pf.equity_aud * (1 - (limit + 0.1) / 100))
    full = replace(pf, open_positions=tuple(
        Position(f"X{i}", 1, 1, 0.9, 1.2, clock.now(), 1) for i in range(cfg.risk.max_open_positions)))
    show("a. Trading paused (/pause)", rm.check_entry(good, pf, mkt, Controls(paused=True)))
    show(f"b. Down {limit + 0.1:.1f}% today (daily loss limit {limit}%)", rm.check_entry(good, down, mkt, ctl))
    show(f"c. {cfg.risk.max_open_positions} positions already open", rm.check_entry(good, full, mkt, ctl))
    show(f"d. {cfg.risk.max_entries_per_day} trades already today",
         rm.check_entry(good, replace(pf, entries_today=cfg.risk.max_entries_per_day), mkt, ctl))
    show("e. Price data is 5 minutes old", rm.check_entry(good, pf, replace(mkt, quote_age_seconds=300), ctl))
    show("f. Major news event (e.g. exchange hack)",
         rm.check_entry(good, pf, mkt, Controls(event_blocks=("exchange hack reported",))))
    show(f"g. Live mode with live.max_capital_aud = A${cfg.live.max_capital_aud:g}",
         rm.check_entry(good, pf, mkt, Controls(live=True, live_capital_cap_aud=cfg.live.max_capital_aud)))

    print("\nSELLING IS NEVER BLOCKED BY PAUSE / KILL / DAILY LIMIT (it only reduces risk)")
    qty = 0.001 if ask > 1000 else 1.0
    held = Position(coin, qty, ask, ask * 0.99, ask * 1.02, clock.now(), ask)
    d = rm.check_exit(held, qty, mkt, reason="stop_loss")   # exits don't even look at pause/kill/daily limit
    print(f"  Stop-loss sell of {qty:g} {coin} while paused, killed and down {limit + 0.1:.1f}%: "
          f"{'APPROVED' if d.approved else 'REJECTED: ' + '; '.join(d.reasons)}")
    print(LINE)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Risk Manager demo on live prices (no AI, no orders)")
    p.add_argument("--coin", default=None, help="coin to use (default: the first coin in config.yaml)")
    p.add_argument("--config", help="path to a config.yaml")
    args = p.parse_args(argv)
    try:
        app = build_app("risk_demo", config_path=args.config, console_logging=False)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2
    try:
        coin = (args.coin or app.config.coins[0].code).upper()
        return run(app, coin)
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 2
    finally:
        app.close()


if __name__ == "__main__":
    sys.exit(main())
