"""Code-based exits: stop-loss, trailing stop, take-profit, max hold time.

This runs every fast check (default every 60s) in plain code. It needs no
AI, so it keeps protecting positions even if Claude is down, the AI budget
is used up, or the bot has been paused or killed.

All checks use the BID price: that's what we'd actually get if we sold now.

Order of checks:
  1. stop (the higher of the stop-loss and the trailing stop)
  2. take-profit
  3. max hold time

A position can carry its own trailing distance and max hold (trend trades
use wide, volatility-based ones); otherwise the exits: settings apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from desk.config import ExitsConfig
from desk.models import Position


@dataclass(frozen=True)
class ExitCheck:
    action: Literal["HOLD", "SELL"]
    reason: str | None            # stop_loss | trailing_stop | take_profit | max_hold
    detail: str
    highest_price: float          # updated highest bid since entry
    trailing_active: bool         # updated trailing state
    effective_stop: float         # the stop actually in force (stop-loss or trailing, whichever is higher)


def trail_distance_pct(pos: Position, cfg: ExitsConfig) -> float:
    return pos.trail_pct if pos.trail_pct is not None else cfg.trailing_distance_pct


def trail_activation_pct(pos: Position, cfg: ExitsConfig) -> float:
    """A position with its own trail distance trails from the moment it's bought."""
    return 0.0 if pos.trail_pct is not None else cfg.trailing_activate_pct


def max_hold(pos: Position, cfg: ExitsConfig) -> float:
    return pos.max_hold_hours if pos.max_hold_hours is not None else cfg.max_hold_hours


def trailing_stop_price(highest: float, cfg: ExitsConfig, distance_pct: float | None = None) -> float:
    d = cfg.trailing_distance_pct if distance_pct is None else distance_pct
    return highest * (1 - d / 100)


def check_exit(pos: Position, bid: float, now: datetime, cfg: ExitsConfig) -> ExitCheck:
    if bid <= 0:
        raise ValueError("bid must be positive")
    dist = trail_distance_pct(pos, cfg)
    highest = max(pos.highest_price, bid)
    trailing = pos.trailing_active or highest >= pos.entry_price * (1 + trail_activation_pct(pos, cfg) / 100)
    trail = trailing_stop_price(highest, cfg, dist) if trailing else None
    stop = max(pos.stop_loss, trail) if trail is not None else pos.stop_loss

    def result(action, reason, detail):
        return ExitCheck(action, reason, detail, highest, trailing, stop)

    if bid <= stop:
        if trail is not None and trail > pos.stop_loss:
            return result("SELL", "trailing_stop",
                          f"bid {bid:.8g} hit the trailing stop {trail:.8g} "
                          f"({dist:g}% below the high of {highest:.8g})")
        return result("SELL", "stop_loss", f"bid {bid:.8g} hit the stop-loss {pos.stop_loss:.8g}")
    if bid >= pos.take_profit:
        return result("SELL", "take_profit", f"bid {bid:.8g} reached the take-profit {pos.take_profit:.8g}")
    limit = max_hold(pos, cfg)
    if now - pos.opened_at >= timedelta(hours=limit):
        return result("SELL", "max_hold", f"held {pos.held_hours(now):.1f}h, the limit is {limit:g}h")
    return result("HOLD", None, "no exit condition met")


def validate_tighten(pos: Position, new_stop: float, bid: float, cfg: ExitsConfig) -> str | None:
    """Check a request to raise a position's stop. Returns a problem, or None if OK.
    Stops only ever move UP, and must stay a little below the current price."""
    if new_stop <= pos.stop_loss:
        return f"new stop {new_stop:.8g} is not above the current stop {pos.stop_loss:.8g} (stops only move up)"
    ceiling = bid * (1 - cfg.tighten_min_gap_pct / 100)
    if new_stop >= ceiling:
        return (f"new stop {new_stop:.8g} is too close to the price {bid:.8g} "
                f"(must be at least {cfg.tighten_min_gap_pct}% below)")
    return None
