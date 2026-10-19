"""Builds the plain-text briefings the AI agents read.

Indicator *levels* come from global (USDT) candles, so they're shown as
percentages relative to price. Every AUD price shown comes from CoinSpot.
"""

from __future__ import annotations

from datetime import datetime

from desk.agents.news_analyst import CoinNews
from desk.config import Config
from desk.models import Position
from desk.scanner import CoinScan, TfView


def _safe(text: str) -> str:
    return str(text).replace("<", "‹").replace(">", "›")


def aud(p: float | None) -> str:
    if p is None:
        return "?"
    return f"A${p:,.2f}" if p >= 100 else f"A${p:,.6g}"


def pct_from(value: float | None, ref: float | None) -> str:
    if value is None or not ref:
        return "?"
    return f"{(ref / value - 1) * 100:+.2f}%"


def round_trip_cost_pct(config: Config, spread_pct: float | None) -> float:
    from desk.risk import round_trip_cost_pct as rt   # one formula for the agents and the Risk Manager
    return rt(config, spread_pct or 0.0)


def _tf_line(v: TfView, config: Config) -> str:
    sc = config.scanner
    parts = []
    if v.rsi is not None:
        prev = f" (previous candle {v.prev_rsi:.1f})" if v.prev_rsi is not None else ""
        parts.append(f"RSI {v.rsi:.1f}{prev}")
    emas = [(sc.ema_fast, v.ema_fast), (sc.ema_mid, v.ema_mid), (sc.ema_slow, v.ema_slow), (sc.ema_trend, v.ema_trend)]
    ema_txt = ", ".join(f"EMA{n} {pct_from(val, v.close)}" for n, val in emas if val is not None)
    if ema_txt:
        parts.append(f"price vs {ema_txt}")
    if v.hist is not None:
        trend = ""
        if v.prev_hist is not None:
            trend = " and rising" if v.hist > v.prev_hist else " and falling"
        parts.append(f"MACD histogram {'positive' if v.hist >= 0 else 'negative'}{trend}")
    if v.atr_pct is not None:
        parts.append(f"ATR {v.atr_pct:.2f}% of price per candle")
    if v.vol_ratio is not None:
        parts.append(f"volume {v.vol_ratio:.1f}x normal")
    return f"- {v.timeframe}: " + "; ".join(parts)


def _news_line(news: CoinNews | None) -> str:
    if news is None:
        return "News mood: not assessed"
    line = f"News mood: {news.sentiment:+.2f} from {news.headline_count} headline(s): {_safe(news.reason)}"
    if news.major_event:
        line += f". MAJOR EVENT: {_safe(news.event_summary)}"
    if news.blocked:
        line += ". New trades in this coin are currently BLOCKED by a major-event rule."
    return line


def trend_context(scan, news: CoinNews | None, config: Config) -> str:
    t = config.strategy.trend
    lines = [f"Coin: {scan.coin}"]
    lines.append(f"CoinSpot now: bid {aud(scan.bid)}, ask {aud(scan.ask)}, spread {scan.spread_pct or 0:.3f}%.")
    lines.append(f"Round-trip trading cost: about {round_trip_cost_pct(config, scan.spread_pct):.2f}% "
                 f"(small next to the multi-day moves this strategy rides)")
    if scan.global_divergence_pct is not None:
        lines.append(f"CoinSpot vs global price: {scan.global_divergence_pct:+.2f}%")
    lines.append(
        f"Strategy: daily trend-following. It buys when a day closes above its {t.breakout_days}-day high while "
        f"above its {t.trend_ema_days}-day average, holds for days to weeks, and exits ONLY through a trailing "
        f"stop {t.stop_atr_multiple:g} daily ATRs below the highest price since buying. There is no fixed "
        f"take-profit: a target shown as +1000% is just a placeholder meaning 'let it run'.")
    if scan.daily_close is not None:
        when = f" (closed {scan.candle_close_time:%Y-%m-%d %H:%M} UTC)" if scan.candle_close_time else ""
        parts = [f"last daily close{when}"]
        if scan.prior_high:
            parts.append(f"{pct_from(scan.prior_high, scan.daily_close)} vs the previous {t.breakout_days}-day high")
        if scan.ema:
            parts.append(f"{pct_from(scan.ema, scan.daily_close)} vs the {t.trend_ema_days}-day average")
        if scan.atr_pct is not None:
            parts.append(f"daily ATR {scan.atr_pct:.2f}% of price")
        lines.append("Daily chart (global prices): " + "; ".join(parts))
    if scan.signal is not None and scan.ask is not None:
        stop = scan.ask * (1 - scan.signal.stop_pct / 100)
        lines.append(f"Signal: YES. Initial stop and trailing stop {scan.signal.stop_pct:.1f}% below "
                     f"({aud(stop)} at today's ask). Rule reasons: {'; '.join(scan.signal.reasons)}")
    else:
        lines.append("Signal: no trend breakout today")
    lines.append(_news_line(news))
    return "\n".join(lines)


def market_context(scan, news: CoinNews | None, config: Config) -> str:
    from desk.trend_scanner import TrendScan
    if isinstance(scan, TrendScan):
        return trend_context(scan, news, config)
    sc = config.scanner
    lines = [f"Coin: {scan.coin}"]
    lines.append(f"CoinSpot now: bid {aud(scan.bid)}, ask {aud(scan.ask)}, spread {scan.spread_pct or 0:.3f}%. "
                 f"A buy fills at about the ask; a sell at about the bid.")
    rt = round_trip_cost_pct(config, scan.spread_pct)
    lines.append(f"Round-trip trading cost: fee {config.fees.market_order_pct}% x2 + spread {scan.spread_pct or 0:.3f}% "
                 f"+ est. slippage {config.fees.slippage_estimate_pct}% x2 = about {rt:.2f}%")
    if scan.global_divergence_pct is not None:
        lines.append(f"CoinSpot vs global price: {scan.global_divergence_pct:+.2f}%")
    lines.append(f"Trend on {sc.trend_timeframe}: {scan.trend}")
    lines.append("Indicators (largest timeframe first):")
    for tf in (sc.trend_timeframe, sc.setup_timeframe, sc.trigger_timeframe):
        if tf in scan.views:
            lines.append(_tf_line(scan.views[tf], config))
    if scan.setups:
        lines.append("Scanner setups:")
        for st in scan.setups:
            stop = scan.ask * (1 - st.suggested_stop_pct / 100) if scan.ask else None
            tgt = scan.ask * (1 + st.suggested_target_pct / 100) if scan.ask else None
            lines.append(f"- {st.kind}: suggested stop -{st.suggested_stop_pct:.2f}% ({aud(stop)}), "
                         f"target +{st.suggested_target_pct:.2f}% ({aud(tgt)})")
            lines += [f"    * {r}" for r in st.reasons]
    else:
        lines.append("Scanner setups: none")
    lines.append(f"Warning signs: {'; '.join(scan.alerts) if scan.alerts else 'none'}")
    if scan.volume_spikes:
        lines.append(f"Volume spikes on: {', '.join(scan.volume_spikes)}")
    lines.append(_news_line(news))
    return "\n".join(lines)


def desk_rules(config: Config) -> str:
    r = config.risk
    if config.strategy.mode == "trend":
        return "\n".join([
            "Desk rules (the hard-coded Risk Manager rejects anything that breaks them):",
            f"- Every position has a stop-loss, at most {r.max_stop_distance_pct:g}% below entry, which then trails.",
            f"- At most {r.max_position_pct:g}% of the account per position and {r.max_open_positions} positions.",
            f"- Confidence below {r.min_confidence} is rejected.",
            f"- New trades stop for the day after a {r.daily_loss_limit_pct:g}% daily loss.",
        ])
    return "\n".join([
        "Desk rules (the hard-coded Risk Manager rejects anything that breaks them):",
        f"- A stop-loss is mandatory. It must be below the entry and at most {r.max_stop_distance_pct}% below it.",
        f"- Reward:risk, (take-profit - entry) / (entry - stop), must be at least {r.min_reward_risk}.",
        f"- The take-profit gain must be at least {r.min_edge_multiple}x the round-trip trading cost.",
        f"- Confidence below {r.min_confidence} is rejected.",
        f"- Entry must be within {r.max_entry_gap_pct}% of the CoinSpot ask (the desk buys at market).",
        f"- Positions are closed automatically after {config.exits.max_hold_hours:g} hours.",
    ])


def portfolio_line(coin: str, open_positions: list[Position], config: Config) -> str:
    held = [p.coin for p in open_positions]
    txt = f"Open positions: {len(held)} of max {config.risk.max_open_positions}"
    if held:
        txt += f" ({', '.join(held)})"
    if coin in held:
        txt += f". The desk ALREADY holds {coin}; only one position per coin is allowed."
    return txt


def position_key(pos: Position) -> str:
    return str(pos.id) if pos.id is not None else f"pretend-{pos.coin}"


def position_context(pos: Position, bid: float, now: datetime, config: Config,
                     effective_stop: float | None = None, trailing_active: bool | None = None) -> str:
    ex = config.exits
    trailing_active = pos.trailing_active if trailing_active is None else trailing_active
    stop = effective_stop if effective_stop is not None else pos.stop_loss
    if trailing_active:
        trail = f"ACTIVE: stop now {aud(stop)}, {ex.trailing_distance_pct}% under the high of {aud(pos.highest_price)}"
    else:
        trail = (f"not active yet (starts once the price is {ex.trailing_activate_pct}% above entry, "
                 f"at {aud(pos.entry_price * (1 + ex.trailing_activate_pct / 100))})")
    return "\n".join([
        f"Position id: {position_key(pos)}",
        f"Holding {pos.qty:.8g} {pos.coin}, bought at {aud(pos.entry_price)} {pos.held_hours(now):.1f} hours ago "
        f"(auto-close after {ex.max_hold_hours:g}h)",
        f"Now: bid {aud(bid)}, {pos.unrealized_pct(bid):+.2f}% vs entry (before fees)",
        f"Stop-loss {aud(pos.stop_loss)} ({(1 - pos.stop_loss / bid) * 100:.2f}% below the bid); "
        f"take-profit {aud(pos.take_profit)} ({(pos.take_profit / bid - 1) * 100:+.2f}% from the bid)",
        f"Trailing stop: {trail}",
    ])
