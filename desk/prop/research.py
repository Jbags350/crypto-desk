"""Prop-firm research report.  prop_research.bat  (python -m desk.prop.research)

Replays the Tradeify 247 2-Step challenge with the recommended strategy from
every week of the last 6 years, using 9 of their top-10 coins, and prints:
  - how often it passes both phases, and how long that takes
  - what a funded $100K account then paid out per month (1 year)
  - the same with pessimistic costs (double spreads and slippage)
  --compare also shows why day trading isn't used: each strategy family's
  result on the tuning years and on the 2 unseen years.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np

from desk.config import load_config
from desk.prop import strategies as S
from desk.prop.challenge import evaluate
from desk.prop.market import PROP_COINS, build_market, load_hourly
from desk.prop.rules import tradeify_2step
from desk.prop.sim import SimConfig, simulate, trades_frame

# The challenge setting (chosen 11 Oct 2026): daily rotation into the 2 strongest coins of the
# last 2 weeks while Bitcoin is above its 50-day average, 1.5% risk per trade, smaller trades
# near the loss floor, and a 2% daily brake (the firm's limit is 3%). The R1 pick (top 3, 1%)
# passes more slowly but essentially never lost the account; use it once funded.
RECOMMENDED = {"strategy": ("rotation", {"lookback": 336, "top": 2, "stop_atr_d": 2.5, "shorts": False}),
               "sim": SimConfig(risk_pct=1.5, max_positions=4, max_same_side=3, daily_brake_pct=2.0,
                                floor_scaling=True)}

COMPARE = [
    ("Day trading: 2-day breakouts", "breakout", {"lookback": 48, "stop_atr": 3.0, "trail_atr": 3.0, "max_hold": 48}),
    ("Day trading: buy dips / sell rallies", "pullback", {}),
    ("Daily trend, long only", "trend", {"stop_atr": 3.0, "shorts": False}),
    ("Daily trend, long + short", "trend", {"stop_atr": 3.0, "shorts": True}),
    ("Rotation, long + short", "rotation", {"lookback": 336, "top": 3, "stop_atr_d": 2.5, "shorts": True}),
    ("Rotation, long only (recommended)", "rotation", {"lookback": 336, "top": 3, "stop_atr_d": 2.5, "shorts": False}),
]


def edge_line(m, sig, rules, a, b) -> str:
    cfg = SimConfig(risk_pct=0.5, max_positions=4, max_same_side=3, daily_brake_pct=None, enforce_rules=False)
    res = simulate(m, sig, rules, cfg, a, b)
    acct = rules.account_size
    years = res.hours / 24 / 365
    eq = res.equity
    peak = np.maximum.accumulate(np.concatenate([[acct], eq]))[1:]
    pnl = np.array([t["pnl"] for t in res.trades]) if res.trades else np.zeros(0)
    gl = -pnl[pnl < 0].sum()
    pf = pnl[pnl > 0].sum() / gl if gl else float("inf")
    return (f"{(eq[-1] / acct - 1) * 100 / years:+6.1f}%/yr  worst drop {((eq - peak) / acct * 100).min():6.1f}%  "
            f"profit factor {pf:4.2f}  {len(pnl) / years:4.0f} trades/yr")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Prop challenge research (Tradeify 247 2-Step)")
    p.add_argument("--size", type=int, default=100_000, choices=[10_000, 25_000, 50_000, 100_000])
    p.add_argument("--compare", action="store_true", help="also compare strategy families (why not day trading)")
    p.add_argument("--offline", action="store_true", help="use downloaded prices only")
    args = p.parse_args(argv)

    config = load_config()
    rules = tradeify_2step(args.size)
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=6 * 365)
    print(f"Loading 6 years of hourly prices for {', '.join(PROP_COINS)} (first run downloads ~5 MB per coin)...")
    frames = load_hourly(PROP_COINS, start - timedelta(days=120), end, config.path("candle_cache"), offline=args.offline)
    m = build_market(frames, start, end)
    split = int(m.index.searchsorted(end - timedelta(days=2 * 365)))

    if args.compare:
        print("\nSTRATEGY FAMILIES (0.5% risk per trade, all of Tradeify 247's costs)")
        print(f"  {'':<38} {'tuning years ' + m.index[0].strftime('%Y') + '-' + m.index[split].strftime('%Y'):<62}"
              f" unseen last 2 years")
        for label, fn, params in COMPARE:
            sig = getattr(S, fn)(m, **params)
            print(f"  {label:<38} {edge_line(m, sig, rules, 0, split):<62} {edge_line(m, sig, rules, split, m.T)}")

    fn, params = RECOMMENDED["strategy"]
    sig = getattr(S, fn)(m, **params)
    cfg = RECOMMENDED["sim"]
    stress = replace(rules, spread_pct={"BTC": 0.04, "ETH": 0.06}, default_spread_pct=0.12, slippage_pct=0.04,
                     stop_slippage_pct=0.10)
    print(f"\n{rules.name} ${args.size:,} (fee ${rules.price_usd:,.0f}): +{rules.targets_pct[0]:g}% then "
          f"+{rules.targets_pct[1]:g}%, {rules.daily_loss_pct:g}% daily / {rules.max_loss_pct:g}% max loss")
    print(f"Strategy: rotation into the {params['top']} strongest coins, {cfg.risk_pct:g}% risk per trade, "
          f"{cfg.daily_brake_pct:g}% daily brake, smaller trades near the loss floor")
    print("Replaying the challenge from every week (this takes a minute or two)...\n")
    for label, r in (("Expected costs", rules), ("Pessimistic costs (2x spread + slippage)", stress)):
        st, atts = evaluate(m, sig, r, cfg, 24 * 30, m.T, every_days=7, phase_max_days=365, funded_days=365)
        n = max(1, len(atts))
        days = np.array([a.days_to_pass for a in atts if a.passed])
        funded = [a for a in atts if a.passed]
        per_month = np.array([a.payouts / max(a.funded_days, 1) * 30.4 for a in funded]) if funded else np.zeros(1)
        lost = sum(1 for a in funded if a.funded_outcome.startswith("breach"))
        print(f"  {label}")
        print(f"    passed both phases: {st.pass_rate:.0f}% of {st.attempts} start dates "
              f"(within 3 / 6 / 12 months: {(days <= 90).sum() / n * 100:.0f}% / {(days <= 180).sum() / n * 100:.0f}% / "
              f"{(days <= 365).sum() / n * 100:.0f}%)")
        print(f"    failed: daily limit {st.fail_daily}, max loss {st.fail_max}, not there after a year {st.fail_timeout}")
        if funded:
            print(f"    funded account: about ${per_month.mean():,.0f} a month to you on average "
                  f"(lost the account within a year: {lost / len(funded) * 100:.0f}%)")
        print(f"    average value per attempt after the ${r.price_usd:,.0f} fee: ${st.ev_per_attempt:+,.0f}\n")

    res = simulate(m, sig, rules, replace(cfg, enforce_rules=False), 24 * 30)
    tf = trades_frame(m, res)
    if not tf.empty:
        print(f"Trading style: about {len(tf) / (res.hours / 24 / 30.4):.0f} trades a month, typically held "
              f"{tf['hours'].median() / 24:.0f} days, {(tf['pnl'] > 0).mean() * 100:.0f}% winners.")
    print("Past results don't guarantee future ones. Tradeify 247 accounts are simulated; payouts are at their discretion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
