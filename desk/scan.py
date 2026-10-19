"""Market scan: run with  scan.bat  (or  python -m desk.scan).

One pass of the desk, without trading:
  Market Scanner -> news fetch -> News Analyst (AI) -> entry debates (AI)
The AI only runs for coins the scanner flagged, unless you ask:
  --score-all            score the news for every coin
  --debate BTC,ETH       run an entry debate for these coins even if not flagged
  --pretend-position BTC pretend the desk holds BTC: show the code exit check
                         and ask the Exit Agent (+ Skeptic) about it
  --no-llm               never call the AI
Read-only: never places orders.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import timedelta

from desk.agents.exit_agent import HeldPosition
from desk.agents.news_analyst import CoinNews, NewsAnalyst
from desk.bootstrap import App, build_app
from desk.clock import Clock
from desk.config import Config, ConfigError
from desk.debate import Debate, DebateResult
from desk.exchange.coinspot import CoinSpotError
from desk.exits import check_exit
from desk.fmt import fmt_num, fmt_price, shorten
from desk.models import Position
from desk.risk import Controls, RiskDecision, RiskManager, record_decision
from desk.risk_inputs import entry_request, fresh_paper_portfolio, market_state
from desk.scanner import CoinScan
from desk.trend_scanner import TrendScanner

LINE = "=" * 78

OUTCOME_TEXT = {
    "approved": "AGENTS AGREE -> the Risk Manager has the final say:",
    "wait": "WAIT: Skeptic says not yet; the desk looks again next cycle",
    "rejected": "REJECTED by the Skeptic",
    "no_trade": "NO TRADE: the Entry Agent passed",
    "hold": "HOLD: keep the position",
    "tighten": "TIGHTEN: the stop-loss would be raised",
    "invalid": "INVALID proposal (broke basic rules; Skeptic not asked)",
    "error": "AI ERROR: treated as no (code stops still protect positions)",
}


def new_cycle_id(clock: Clock) -> str:
    return clock.now().strftime("%Y%m%dT%H%M%SZ")


def momentum(view) -> str:
    """MACD histogram as words: sign (above/below signal) and direction."""
    if view is None or view.hist is None:
        return "-"
    side = "+" if view.hist >= 0 else "-"
    if view.prev_hist is None:
        return side
    return f"{side} {'rising' if view.hist > view.prev_hist else 'falling'}"


def wrap(text: str, indent: int = 8) -> str:
    return textwrap.fill(text, width=100, initial_indent=" " * indent, subsequent_indent=" " * indent)


def pretend_position(scan: CoinScan, config: Config, clock: Clock) -> Position:
    """A make-believe position: bought 0.5% below the current ask, 3 hours ago, A$500 worth."""
    entry = scan.ask * 0.995
    s = scan.views.get(config.scanner.setup_timeframe)
    atr = s.atr_pct if s and s.atr_pct else 1.0
    stop = entry * (1 - atr * config.scanner.stop_atr_multiple / 100)
    tp = entry * (1 + atr * config.scanner.target_atr_multiple / 100)
    return Position(coin=scan.coin, qty=500 / entry, entry_price=entry, stop_loss=stop, take_profit=tp,
                    opened_at=clock.now() - timedelta(hours=3), highest_price=max(entry, scan.bid or entry))


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def print_trend(scans, config: Config) -> None:
    t = config.strategy.trend
    print(f"\nTREND CHECK (strategy.mode: trend - the bot acts on this once a day, after the daily close at "
          f"10:00 Brisbane)")
    print(f"  Buy signal = daily close above its {t.breakout_days}-day high AND above its {t.trend_ema_days}-day average")
    print(f"  {'Coin':<5}{'Price AUD':>13}  {'vs ' + str(t.breakout_days) + '-day high':>16}{'vs ' + str(t.trend_ema_days) + '-day avg':>15}"
          f"{'daily ATR':>10}  Signal")
    for s in scans:
        hi = f"{(s.daily_close / s.prior_high - 1) * 100:+.1f}%" if s.daily_close and s.prior_high else "-"
        av = f"{(s.daily_close / s.ema - 1) * 100:+.1f}%" if s.daily_close and s.ema else "-"
        atr = f"{s.atr_pct:.2f}%" if s.atr_pct is not None else "-"
        sig = "BUY SIGNAL" if s.flagged else ("(blocked: data problem)" if s.signal else "no")
        print(f"  {s.coin:<5}{fmt_price(s.price_aud):>13}  {hi:>16}{av:>15}{atr:>10}  {sig}")
        for p in s.problems:
            print(f"        ! {p}")
        if s.signal:
            print(f"        stop and trailing stop {s.signal.stop_pct:.1f}% below the price; no fixed take-profit")
    print("  (Signals use the last CLOSED daily candle. The intraday scanner below is for information only.)")


def print_scanner(scans: list[CoinScan], config: Config) -> None:
    sc = config.scanner
    tfs = [sc.trigger_timeframe, sc.setup_timeframe, sc.trend_timeframe]
    print(f"\nMARKET SCANNER   (trend = {sc.trend_timeframe}, setups = {sc.setup_timeframe}, "
          f"trigger = {sc.trigger_timeframe})")
    print(f"  {'Coin':<5}{'Price AUD':>13}{'Spread':>8}  {'Trend':<6}{'RSI ' + '/'.join(tfs):>17}"
          f"  {'MACD':<10}{'ATR':>6}{'Volume':>8}  Setups")
    for r in scans:
        s = r.views.get(sc.setup_timeframe)
        rsis = " / ".join(fmt_num(r.views[tf].rsi, ".0f") if tf in r.views else "-" for tf in tfs)
        setups = ", ".join(x.kind for x in r.setups) or ("(blocked)" if r.suppressed_setups else "-")
        print(f"  {r.coin:<5}{fmt_price(r.price_aud):>13}{fmt_num(r.spread_pct, '.2f') + '%':>8}  {r.trend:<6}"
              f"{rsis:>17}  {momentum(s):<10}"
              f"{fmt_num(s.atr_pct if s else None, '.2f') + '%':>6}"
              f"{fmt_num(s.vol_ratio if s else None, '.1f') + 'x':>8}  {setups}")
    print(f"  (MACD, ATR and Volume use the {sc.setup_timeframe} chart. MACD + = upward momentum. "
          f"ATR = typical candle size. Volume = vs normal.)")
    for r in scans:
        for st in r.setups + r.suppressed_setups:
            tag = "" if st in r.setups else "  [BLOCKED: data problem]"
            print(f"\n  >> {r.coin}: {st.kind}{tag}   suggested stop -{st.suggested_stop_pct:.2f}%, "
                  f"target +{st.suggested_target_pct:.2f}%")
            for why in st.reasons:
                print(f"       - {why}")
        if r.problems:
            print(f"\n  !! {r.coin} data problems (no trading until fixed):")
            for p in r.problems:
                print(f"       - {p}")
        if r.alerts:
            print(f"\n  .. {r.coin} warning signs (only matter if we hold it): {'; '.join(r.alerts)}")
    if not any(r.flagged for r in scans):
        print("\n  No setups right now. That's normal; most scans find nothing.")


def print_entry_debate(r: DebateResult) -> None:
    print(f"\n  {r.coin} ----------------------------------------------------------------")
    p = r.proposal
    if p and p["decision"] == "BUY":
        e, s, t = p["entry_aud"], p["stop_loss_aud"], p["take_profit_aud"]
        rr = (t - e) / (e - s) if e > s else float("nan")
        print(f"    Entry Agent: BUY at {fmt_price(e)}  stop {fmt_price(s)} ({(s / e - 1) * 100:+.2f}%)  "
              f"target {fmt_price(t)} ({(t / e - 1) * 100:+.2f}%)  reward:risk {rr:.2f}  "
              f"confidence {p['confidence']}")
        print(wrap(p["reasoning"]))
    elif p:
        print(f"    Entry Agent: NO TRADE (confidence {p['confidence']})")
        print(wrap(p["reasoning"]))
    if r.verdict:
        print(f"    Skeptic votes: {r.verdict['vote']}")
        for a in r.verdict["arguments_against"]:
            print(wrap(f"- {a}", 8))
        print(wrap(r.verdict["reasoning"]))
    print(f"    => {OUTCOME_TEXT.get(r.outcome, r.outcome)}")
    for pr in r.problems:
        print(wrap(f"! {pr}", 8))


def print_risk(d: RiskDecision, note: str = "") -> None:
    if d.approved:
        verb = "buy" if d.kind == "entry" else "sell"
        print(f"    Risk Manager: APPROVED - would {verb} {d.qty:.8g} {d.coin} = A${d.notional_aud:,.2f} "
              f"+ fee A${d.fee_aud:,.2f}{note}  (nothing is traded by scan.bat)")
    else:
        print("    Risk Manager: REJECTED")
        for c in d.failures:
            print(wrap(f"x {c.name}: {c.detail}", 8))


def print_exit_debate(r: DebateResult) -> None:
    a = r.proposal
    if a:
        extra = f" new stop {fmt_price(a['new_stop_aud'])}" if a["action"] == "TIGHTEN_STOP" else ""
        print(f"    Exit Agent: {a['action']}{extra} (confidence {a['confidence']})")
        print(wrap(a["reasoning"]))
    if r.verdict:
        print(f"    Skeptic votes: {r.verdict['vote']}")
        for arg in r.verdict["arguments_against"]:
            print(wrap(f"- {arg}", 8))
        print(wrap(r.verdict["reasoning"]))
    exit_text = {
        "approved": "AGENTS AGREE TO SELL -> the Risk Manager has the final say:",
        "wait": "WAIT: keep it for now; the desk re-checks next cycle",
        "rejected": "Skeptic rejected the sale: keep the position",
    }
    text = exit_text.get(r.outcome) or OUTCOME_TEXT.get(r.outcome, r.outcome)
    print(f"    => {text}")
    for pr in r.problems:
        print(wrap(f"! {pr}", 8))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(app: App, *, score_all: bool = False, use_llm: bool = True, coins: list[str] | None = None,
        debate_coins: list[str] | None = None, pretend_coins: list[str] | None = None) -> int:
    cfg, clock = app.config, app.clock
    coin_cfgs = [cfg.coin(c) for c in coins] if coins else list(cfg.coins)
    for extra in (debate_coins or []) + (pretend_coins or []):
        if extra not in [c.code for c in coin_cfgs]:
            coin_cfgs.append(cfg.coin(extra))
    cycle_id = new_cycle_id(clock)

    print(LINE)
    print(f" Crypto Desk - market scan        {clock.local_now():%Y-%m-%d %H:%M %Z}   (cycle {cycle_id})")
    print(LINE)

    try:
        snap = app.market.snapshot()
    except CoinSpotError as exc:
        print(f"CoinSpot prices unavailable: {exc.message}. Nothing can be traded right now.")
        return 1
    if cfg.strategy.mode == "trend":
        print_trend(TrendScanner(cfg, app.candles, clock).scan(snap, coin_cfgs), cfg)
    scans = {r.coin: r for r in app.scanner.scan(snap, coin_cfgs, cycle_id=cycle_id)}
    print_scanner(list(scans.values()), cfg)

    # ---- News -----------------------------------------------------------
    rep = app.news.fetch_all()
    print(f"\nNEWS   ({rep.fetched} headlines read, {rep.new} new)")
    print("  " + " | ".join(f"{src} {n}" for src, n in rep.per_source.items()))
    for e in rep.errors:
        print(f"  feed problem: {e}")
    for b in rep.exchange_blocks:
        print(f"  !! ALL TRADING BLOCKED: {b}")
    for c in coin_cfgs:
        items = app.news.recent(c.code, limit=cfg.news.max_headlines_per_coin)
        print(f"\n  {c.code}: {len(items)} headline(s) in the last {cfg.news.max_age_hours:g}h")
        for it in items[:4]:
            ev = f"  [possible {', '.join(it.event_kinds)}]" if it.event_kinds else ""
            print(f"    {it.published_at.astimezone(cfg.tz):%H:%M}  {shorten(it.source, 18):<18} "
                  f"{shorten(it.title, 80)}{ev}")
    blocks = app.news.active_blocks()
    print(f"\n  Active trading blocks: {'none' if not blocks else ''}")
    for b in blocks:
        print(f"    {b['coin']}: {b['summary']}  (until {b['block_until'][:16]} UTC)")

    # ---- AI agents ------------------------------------------------------------
    flagged = [c for c, r in scans.items() if r.flagged]
    debate_targets = list(dict.fromkeys(flagged + [c for c in (debate_coins or []) if c in scans]))
    pretend_targets = [c for c in (pretend_coins or []) if c in scans and scans[c].ask]
    news_targets = [c.code for c in coin_cfgs] if score_all else \
        list(dict.fromkeys(debate_targets + pretend_targets))

    ai_on = use_llm and app.llm.available
    spent_before = app.llm.spent_today_usd() if ai_on else 0.0
    print("\nNEWS ANALYST (AI)")
    news: dict[str, CoinNews] = {}
    if not use_llm:
        print("  Skipped (--no-llm).")
    elif not app.llm.available:
        print("  Off: no ANTHROPIC_API_KEY in .env. Add one to turn the AI agents on.")
    elif not news_targets:
        print("  Not called: no coin was flagged, so there's nothing to analyse and no AI cost.")
        print("  (Try  scan.bat --score-all  or  scan.bat --debate BTC  to see the AI anyway.)")
    else:
        news = NewsAnalyst(cfg, app.news, app.llm, app.db, clock).assess(news_targets, cycle_id=cycle_id)
        print(f"  {'Coin':<5}{'Mood':>7}  {'Major event?':<13}{'Source':<9}Reason")
        for cn in news.values():
            major = "YES" if cn.major_event else ("blocked" if cn.blocked else "no")
            print(f"  {cn.coin:<5}{cn.sentiment:>+7.2f}  {major:<13}{cn.source:<9}{shorten(cn.reason, 70)}")
            if cn.major_event:
                print(f"        event: {cn.event_summary}")

    debate = Debate(cfg, app.llm, app.db, clock)
    rm = RiskManager(cfg)
    if debate_targets:
        print("\nENTRY DEBATES (AI: Entry Agent proposes, Skeptic challenges)")
        if not ai_on:
            print("  Skipped: the AI is off.")
        for coin in debate_targets if ai_on else []:
            if coin not in flagged:
                print(f"\n  (note: {coin} was not flagged by the scanner; debating it because you asked)")
            r = debate.run_entry(scans[coin], news.get(coin), [], cycle_id=cycle_id)
            print_entry_debate(r)
            if r.proposal and r.proposal["decision"] == "BUY" and r.outcome not in ("invalid", "error"):
                d = rm.check_entry(
                    entry_request(r.proposal, (r.verdict or {}).get("vote")), fresh_paper_portfolio(cfg),
                    market_state(scans[coin], clock.now(), app.coinspot.health.consecutive_errors),
                    Controls(event_blocks=tuple(b["summary"] for b in app.news.active_blocks(coin))))
                record_decision(app.db, clock, d, cycle_id=cycle_id, debate_id=r.id)
                print_risk(d, f" on a fresh A${cfg.paper.starting_balance_aud:,.0f} paper account")

    if pretend_targets:
        print("\nPRETEND POSITIONS (nothing is bought; shows what the exit side would do)")
        for coin in pretend_targets:
            scan = scans[coin]
            pos = pretend_position(scan, cfg, clock)
            chk = check_exit(pos, scan.bid, clock.now(), cfg.exits)
            print(f"\n  {coin}: pretend bought {pos.qty:.6g} at {fmt_price(pos.entry_price)} 3h ago | "
                  f"stop {fmt_price(pos.stop_loss)} | target {fmt_price(pos.take_profit)} | "
                  f"now bid {fmt_price(scan.bid)} ({pos.unrealized_pct(scan.bid):+.2f}%)")
            trail = (f"trailing stop active at {fmt_price(chk.effective_stop)}" if chk.trailing_active
                     else "trailing stop not active yet")
            print(f"    Code exit check (runs every {cfg.schedule.fast_check_seconds}s, no AI): "
                  f"{chk.action}{' - ' + chk.reason if chk.reason else ''} ({trail})")
            if not ai_on:
                print("    Exit Agent skipped: the AI is off.")
                continue
            held = HeldPosition(pos, scan.bid, scan, news.get(coin), chk.effective_stop, chk.trailing_active)
            for r in debate.run_exits([held], clock.now(), cycle_id=cycle_id, force=True):
                print_exit_debate(r)
                if r.outcome == "approved":
                    d = rm.check_exit(pos, pos.qty, market_state(scan, clock.now(),
                                                                 app.coinspot.health.consecutive_errors),
                                      reason="agent_sell", skeptic_vote=(r.verdict or {}).get("vote"))
                    record_decision(app.db, clock, d, cycle_id=cycle_id, debate_id=r.id, reason="agent_sell")
                    print_risk(d)

    if ai_on and (news_targets or debate_targets or pretend_targets):
        spent = app.llm.spent_today_usd()
        print(f"\n  AI cost: US${spent - spent_before:.4f} this scan, "
              f"US${spent:.4f} today (daily cap US${cfg.llm.daily_spend_cap_usd:.2f})")
    print(LINE)
    return 0


def _codes(text: str | None) -> list[str]:
    return [c.strip().upper() for c in text.split(",") if c.strip()] if text else []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the desk once without trading (read-only)")
    p.add_argument("--score-all", action="store_true", help="ask the AI to score news for every coin")
    p.add_argument("--debate", help="run entry debates for these coins even if not flagged, e.g. BTC,ETH")
    p.add_argument("--pretend-position", help="pretend the desk holds these coins, e.g. BTC")
    p.add_argument("--no-llm", action="store_true", help="never call the AI")
    p.add_argument("--coins", help="only scan these coins, e.g. BTC,ETH (default: all in config)")
    p.add_argument("--config", help="path to a config.yaml")
    args = p.parse_args(argv)
    try:
        app = build_app("scan", config_path=args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2
    try:
        return run(app, score_all=args.score_all, use_llm=not args.no_llm, coins=_codes(args.coins) or None,
                   debate_coins=_codes(args.debate), pretend_coins=_codes(args.pretend_position))
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 2
    finally:
        app.close()


if __name__ == "__main__":
    sys.exit(main())
