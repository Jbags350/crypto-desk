"""Risk Manager: hard-coded rules with the final veto. No AI, no opinions.

Every trade the agents agree on comes here last. The Risk Manager checks
EVERY rule (it doesn't stop at the first failure), so the log always shows
all the reasons a trade was refused. A trade goes ahead only if every check
passes. All limits come from config.yaml -> risk / fees / data / live.

New positions (entries) must pass:
  stop_loss_present      every trade MUST have a stop-loss
  take_profit_present    and a take-profit
  valid_numbers          every price/amount is a real positive number
  known_coin             the coin is in the config list
  kill_switch            /kill is not active
  not_paused             /pause is not active
  daily_loss_limit       today's loss hasn't reached the limit (then paused until local midnight)
  max_entries_per_day    not too many new trades today
  max_open_positions     not too many positions open
  one_position_per_coin  not already holding this coin
  fresh_price            the CoinSpot price is recent
  data_healthy           the scanner found no data problems (stale candles, price divergence...)
  api_healthy            CoinSpot isn't failing repeatedly
  no_major_event         no hack/outage/regulation block on this coin
  skeptic_agreed         the Skeptic voted BUY
  min_confidence         the Entry Agent's confidence is high enough
  entry_near_market      the proposed entry matches the CoinSpot ask (we buy at market)
  stop_loss_below_entry  a stop-loss exists and is below the price we'd pay
  stop_distance          the stop isn't too far away
  take_profit_above_entry
  reward_risk            (take-profit gain) / (stop-loss loss) is big enough
  beats_costs            the take-profit gain is a multiple of fees + spread + slippage
  position_size          the size (max X% of the portfolio, limited by cash and, when live,
                         the live capital cap) is at least the minimum order

Selling an existing position (exits) is about REDUCING risk, so it is never
blocked by pause, kill, the daily loss limit, news blocks or data warnings.
It only needs a sane quantity and a fresh price. A discretionary AI sell
also needs the Skeptic's SELL vote. Code stops (stop-loss, trailing stop,
take-profit, max hold) and manual closes don't.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date

from desk.clock import Clock, iso
from desk.config import Config
from desk.db import Database
from desk.models import Position

EPS = 1e-9          # tolerance so exact boundary values (e.g. reward:risk exactly 1.5) pass
COIN_DECIMALS = 8   # CoinSpot accepts up to 8 decimal places for coin amounts

PROTECTIVE_EXIT_REASONS = ("stop_loss", "trailing_stop", "take_profit", "max_hold", "kill_close", "manual_close")


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _positive(x) -> bool:
    return _finite(x) and x > 0


def round_trip_cost_pct(config: Config, spread_pct: float) -> float:
    """Fees + spread + slippage for a buy and a sell, in %, measured from the mid price.
    A market order pays half the spread plus slippage; a limit order resting at the
    bid (buy) or at the target (sell) pays neither. Both pay CoinSpot's fee."""
    f, ex = config.fees, config.execution
    market_legs = (ex.entry_order == "market") + (ex.take_profit_order == "market")
    return 2 * f.market_order_pct + market_legs * (spread_pct / 2 + f.slippage_estimate_pct)


def daily_pause_active(paused_on: date | None, today: date) -> bool:
    """The daily loss pause lasts until local midnight: it's active only on the day it was hit."""
    return paused_on is not None and paused_on == today


def floor_qty(qty: float) -> float:
    """Round a coin amount DOWN to CoinSpot's 8 decimals (never buy more than budgeted)."""
    if not _finite(qty) or qty <= 0:
        return 0.0
    return math.floor(qty * 10 ** COIN_DECIMALS) / 10 ** COIN_DECIMALS


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PortfolioState:
    equity_aud: float                  # cash + open positions valued at the bid
    cash_aud: float                    # AUD available to spend
    day_start_equity_aud: float        # equity at local midnight
    open_positions: tuple[Position, ...] = ()
    entries_today: int = 0             # new positions opened today (local day)
    live_exposure_aud: float = 0.0     # cost of open LIVE positions (for the live capital cap)

    @property
    def daily_pnl_pct(self) -> float:
        """Today's profit/loss in %, realised + unrealised (equity vs midnight)."""
        if not _positive(self.day_start_equity_aud) or not _finite(self.equity_aud):
            return float("nan")
        return (self.equity_aud / self.day_start_equity_aud - 1) * 100


@dataclass(frozen=True)
class MarketState:
    coin: str
    bid: float
    ask: float
    quote_age_seconds: float               # how old the CoinSpot price is
    data_problems: tuple[str, ...] = ()    # from the scanner
    api_error_streak: int = 0              # CoinSpot failures in a row


@dataclass(frozen=True)
class Controls:
    killed: bool = False
    paused: bool = False
    daily_loss_paused: bool = False        # the daily limit was already hit today
    event_blocks: tuple[str, ...] = ()     # active major-event blocks for this coin
    live: bool = False
    live_capital_cap_aud: float | None = None


@dataclass(frozen=True)
class EntryRequest:
    coin: str
    entry_aud: float
    stop_loss_aud: float
    take_profit_aud: float
    confidence: float
    skeptic_vote: str | None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskDecision:
    kind: str                         # entry | exit
    coin: str
    approved: bool
    checks: tuple[Check, ...]
    qty: float = 0.0                  # coins to buy/sell
    price: float = 0.0                # expected price (ask for buys, bid for sells)
    max_price: float = 0.0            # buys: never pay more than this (the size was worked out at this price)
    notional_aud: float = 0.0         # qty x price
    fee_aud: float = 0.0              # expected CoinSpot fee
    stop_loss_aud: float = 0.0
    take_profit_aud: float = 0.0
    daily_limit_hit: bool = False     # tells the caller to pause entries for the rest of the day
    metrics: dict = field(default_factory=dict)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.failures]

    def summary(self) -> str:
        if self.approved:
            return (f"APPROVED {self.kind} {self.coin}: {self.qty:.8g} @ {self.price:.8g} "
                    f"= A${self.notional_aud:,.2f} (+ fee A${self.fee_aud:,.2f})")
        return f"REJECTED {self.kind} {self.coin}: " + "; ".join(self.reasons)


# ---------------------------------------------------------------------------
# The Risk Manager
# ---------------------------------------------------------------------------

class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    @property
    def fee_rate(self) -> float:
        return self.config.fees.market_order_pct / 100

    def round_trip_cost_pct(self, spread_pct: float) -> float:
        return round_trip_cost_pct(self.config, spread_pct)

    def daily_loss_hit(self, pf: PortfolioState) -> bool:
        pnl = pf.daily_pnl_pct
        return _finite(pnl) and pnl <= -self.config.risk.daily_loss_limit_pct + EPS

    # ---- entries -------------------------------------------------------
    def check_entry(self, req: EntryRequest, pf: PortfolioState, mkt: MarketState, ctl: Controls,
                    entry_price: float | None = None) -> RiskDecision:
        """entry_price: the price we'd actually pay. Default: the ask (a market order).
        A limit order resting at the bid passes the bid, and the order may never pay more."""
        cfg, r = self.config, self.config.risk
        checks: list[Check] = []

        def add(name: str, ok, detail: str) -> bool:
            checks.append(Check(name, bool(ok), detail))
            return bool(ok)

        has_stop = add("stop_loss_present", _positive(req.stop_loss_aud),
                       "stop-loss given" if _positive(req.stop_loss_aud)
                       else "NO STOP-LOSS: every trade must have one")
        has_tp = add("take_profit_present", _positive(req.take_profit_aud),
                     "take-profit given" if _positive(req.take_profit_aud) else "no take-profit given")
        numbers_ok = add(
            "valid_numbers",
            all(_positive(x) for x in (req.entry_aud, mkt.bid, mkt.ask, pf.equity_aud, pf.day_start_equity_aud))
            and _finite(req.confidence) and _finite(pf.cash_aud) and pf.cash_aud >= 0
            and _finite(mkt.quote_age_seconds) and mkt.bid <= mkt.ask,
            "entry, bid/ask, equity and cash are real numbers and bid <= ask",
        ) and has_stop and has_tp
        known = [c.code for c in cfg.coins]
        add("known_coin", req.coin in known and req.coin == mkt.coin,
            f"{req.coin} must be in the config coin list and match the market data ({mkt.coin})")
        add("kill_switch", not ctl.killed, "kill switch is ACTIVE" if ctl.killed else "off")
        add("not_paused", not ctl.paused, "trading is PAUSED" if ctl.paused else "running")

        pnl = pf.daily_pnl_pct
        limit_hit = ctl.daily_loss_paused or not _finite(pnl) or pnl <= -r.daily_loss_limit_pct + EPS
        add("daily_loss_limit", not limit_hit,
            ("already hit today; paused until midnight" if ctl.daily_loss_paused else
             f"today {pnl:+.2f}% (limit -{r.daily_loss_limit_pct}%)" if _finite(pnl) else "today's P&L unknown"))
        add("max_entries_per_day", pf.entries_today < r.max_entries_per_day,
            f"{pf.entries_today} new trade(s) today, max {r.max_entries_per_day}")
        add("max_open_positions", len(pf.open_positions) < r.max_open_positions,
            f"{len(pf.open_positions)} open, max {r.max_open_positions}")
        held = [p.coin for p in pf.open_positions]
        add("one_position_per_coin", req.coin not in held,
            f"already holding {req.coin}" if req.coin in held else f"not holding {req.coin}")
        add("fresh_price", _finite(mkt.quote_age_seconds) and 0 <= mkt.quote_age_seconds <= cfg.data.max_price_age_seconds,
            f"price is {mkt.quote_age_seconds:.0f}s old (max {cfg.data.max_price_age_seconds}s)"
            if _finite(mkt.quote_age_seconds) else "price age unknown")
        add("data_healthy", not mkt.data_problems,
            "; ".join(mkt.data_problems) if mkt.data_problems else "no data problems")
        add("api_healthy", mkt.api_error_streak < cfg.data.api_error_streak_limit,
            f"{mkt.api_error_streak} CoinSpot error(s) in a row (limit {cfg.data.api_error_streak_limit})")
        add("no_major_event", not ctl.event_blocks,
            "; ".join(ctl.event_blocks) if ctl.event_blocks else "no active event blocks")
        add("skeptic_agreed", req.skeptic_vote == "BUY", f"Skeptic voted {req.skeptic_vote or 'nothing'}")
        add("min_confidence", _finite(req.confidence) and req.confidence >= r.min_confidence,
            f"confidence {req.confidence} (min {r.min_confidence})")

        metrics: dict = {"daily_pnl_pct": pnl if _finite(pnl) else None}
        qty = notional = fee = 0.0
        if not numbers_ok:
            add("price_checks", False, "stop/target/size checks skipped because some numbers are missing or invalid")
        else:
            limit_entry = entry_price is not None
            bid, stop, tp = mkt.bid, req.stop_loss_aud, req.take_profit_aud
            ask = entry_price if limit_entry and _positive(entry_price) else mkt.ask   # the price we'd pay
            gap = abs(req.entry_aud / ask - 1) * 100
            add("entry_near_market", gap <= r.max_entry_gap_pct + EPS and (not limit_entry or bid <= ask <= mkt.ask),
                f"proposed entry is {gap:.2f}% from the {'limit' if limit_entry else 'ask'} price "
                f"(max {r.max_entry_gap_pct}%)")
            stop_ok = add("stop_loss_below_entry", stop < ask,
                          f"stop {stop:.8g} vs ask {ask:.8g}")
            stop_dist = (1 - stop / ask) * 100
            add("stop_distance", stop_ok and stop_dist <= r.max_stop_distance_pct + EPS,
                f"stop is {stop_dist:.2f}% below the ask (max {r.max_stop_distance_pct}%)")
            tp_ok = add("take_profit_above_entry", tp > ask, f"take-profit {tp:.8g} vs ask {ask:.8g}")
            rr = (tp - ask) / (ask - stop) if stop_ok and tp_ok else 0.0
            add("reward_risk", stop_ok and tp_ok and rr >= r.min_reward_risk - EPS,
                f"reward:risk {rr:.2f} (min {r.min_reward_risk})")
            spread = (mkt.ask - bid) / ((mkt.ask + bid) / 2) * 100
            cost = self.round_trip_cost_pct(spread)
            gain = (tp / ask - 1) * 100
            need = r.min_edge_multiple * cost
            add("beats_costs", tp_ok and gain >= need - EPS,
                f"take-profit gain {gain:.2f}% vs needed {need:.2f}% "
                f"({r.min_edge_multiple}x round-trip cost {cost:.2f}%)")

            # Position size: the smallest of the limits, fee included
            limits = {f"{r.max_position_pct}% of portfolio": pf.equity_aud * r.max_position_pct / 100,
                      "available cash": pf.cash_aud}
            live_ok = True
            if ctl.live:
                if ctl.live_capital_cap_aud is None or not _positive(ctl.live_capital_cap_aud):
                    live_ok = False
                else:
                    limits["live capital cap"] = max(0.0, ctl.live_capital_cap_aud - pf.live_exposure_aud)
            binding = min(limits, key=limits.get)
            spend = max(0.0, limits[binding])
            # Size at the WORST price the executor will accept (ask + max_entry_gap_pct; a limit order
            # never pays more than its limit), so even a fill with slippage can never cost more than
            # the limit. Expected cost is shown at the entry price.
            max_price = ask if limit_entry else ask * (1 + r.max_entry_gap_pct / 100)
            qty = floor_qty(spend / (1 + self.fee_rate) / max_price)
            notional = qty * ask
            fee = notional * self.fee_rate
            if not live_ok:
                add("position_size", False, "live trading needs live.max_capital_aud set in config.yaml")
            else:
                add("position_size", notional >= r.min_order_aud,
                    f"A${notional:,.2f} + fee A${fee:,.2f} (limited by {binding}; min order A${r.min_order_aud:g})")
            metrics.update({"spread_pct": spread, "round_trip_cost_pct": cost, "gain_pct": gain,
                            "stop_distance_pct": stop_dist, "reward_risk": rr, "size_limited_by": binding,
                            "position_pct_of_equity": (notional + fee) / pf.equity_aud * 100,
                            "max_fill_price": max_price})

        approved = all(c.passed for c in checks)
        return RiskDecision(
            kind="entry", coin=req.coin, approved=approved, checks=tuple(checks),
            qty=qty if approved else 0.0,
            price=(entry_price if entry_price is not None else mkt.ask) if numbers_ok else 0.0,
            max_price=metrics.get("max_fill_price", 0.0) if approved else 0.0,
            notional_aud=notional if approved else 0.0, fee_aud=fee if approved else 0.0,
            stop_loss_aud=req.stop_loss_aud, take_profit_aud=req.take_profit_aud,
            daily_limit_hit=self.daily_loss_hit(pf) and not ctl.daily_loss_paused, metrics=metrics,
        )

    # ---- exits -------------------------------------------------------------
    def check_exit(self, pos: Position, qty: float, mkt: MarketState, *, reason: str,
                   skeptic_vote: str | None = None) -> RiskDecision:
        cfg = self.config
        checks: list[Check] = []

        def add(name: str, ok, detail: str) -> bool:
            checks.append(Check(name, bool(ok), detail))
            return bool(ok)

        add("valid_numbers", _positive(qty) and _positive(mkt.bid) and _positive(pos.qty),
            "quantity and bid are real positive numbers")
        add("same_coin", pos.coin == mkt.coin, f"position {pos.coin}, price for {mkt.coin}")
        add("not_more_than_held", _finite(qty) and qty <= pos.qty + EPS,
            f"selling {qty:.8g} of {pos.qty:.8g} held (the bot only sells what it bought)")
        add("fresh_price", _finite(mkt.quote_age_seconds) and 0 <= mkt.quote_age_seconds <= cfg.data.max_price_age_seconds,
            f"price is {mkt.quote_age_seconds:.0f}s old (max {cfg.data.max_price_age_seconds}s); retry with a fresh price"
            if _finite(mkt.quote_age_seconds) else "price age unknown")
        protective = reason in PROTECTIVE_EXIT_REASONS
        if not protective:
            add("skeptic_agreed", skeptic_vote == "SELL",
                f"discretionary sell ({reason}) needs the Skeptic's SELL vote; got {skeptic_vote or 'nothing'}")
        approved = all(c.passed for c in checks)
        notional = qty * mkt.bid if approved else 0.0
        return RiskDecision(
            kind="exit", coin=pos.coin, approved=approved, checks=tuple(checks),
            qty=qty if approved else 0.0, price=mkt.bid if _positive(mkt.bid) else 0.0,
            notional_aud=notional, fee_aud=notional * self.fee_rate,
            stop_loss_aud=pos.stop_loss, take_profit_aud=pos.take_profit,
            metrics={"reason": reason, "protective": protective},
        )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def record_decision(db: Database, clock: Clock, d: RiskDecision, *, cycle_id: str | None = None,
                    debate_id: int | None = None, reason: str | None = None) -> int:
    cur = db.execute(
        "INSERT INTO risk_decisions(ts, cycle_id, kind, coin, approved, reason, qty, price, notional_aud, "
        "fee_aud, failures, checks, metrics, debate_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (iso(clock.now()), cycle_id, d.kind, d.coin, int(d.approved), reason, d.qty, d.price, d.notional_aud,
         d.fee_aud, json.dumps(d.reasons), json.dumps([asdict(c) for c in d.checks]),
         json.dumps(d.metrics, default=str), debate_id),
    )
    return int(cur.lastrowid)
