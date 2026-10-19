"""Strategy modes: day (the original), swing (bigger moves) and trend (daily trend-following).

apply_strategy() turns config.yaml into the settings a mode actually uses, so
the scanner, exits and Risk Manager need no special cases for swing mode.
Trend mode uses its own daily signal (desk/trend.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from desk.config import Config

LIVE_READY_MODES = {"day", "trend"}
LIVE_READY_ORDERS = {"market"}


@dataclass(frozen=True)
class Signal:
    """A reason to buy, with the exits the trade should use."""
    coin: str
    kind: str
    stop_pct: float                       # stop-loss distance below entry, %
    target_pct: float                     # take-profit distance above entry, %
    reasons: tuple[str, ...] = field(default_factory=tuple)
    trail_pct: float | None = None        # per-trade trailing distance (None = config exits)
    max_hold_hours: float | None = None   # per-trade max hold (None = config exits)


def apply_strategy(config: Config) -> Config:
    """The effective config for config.strategy.mode."""
    mode = config.strategy.mode
    if mode == "swing":
        s = config.strategy.swing
        scanner = config.scanner.model_copy(update={
            "trend_timeframe": s.trend_timeframe, "setup_timeframe": s.setup_timeframe,
            "trigger_timeframe": s.trigger_timeframe, "stop_atr_multiple": s.stop_atr_multiple,
            "target_atr_multiple": s.target_atr_multiple})
        exits = config.exits.model_copy(update={
            "trailing_activate_pct": s.trailing_activate_pct, "trailing_distance_pct": s.trailing_distance_pct,
            "max_hold_hours": s.max_hold_hours})
        tfs = sorted({s.trend_timeframe, s.setup_timeframe, s.trigger_timeframe}
                     | set(config.data.timeframes))
        return config.model_copy(update={"scanner": scanner, "exits": exits,
                                         "data": config.data.model_copy(update={"timeframes": tfs})})
    if mode == "trend":
        t = config.strategy.trend
        exits = config.exits.model_copy(update={"max_hold_hours": t.max_hold_days * 24})
        risk = config.risk.model_copy(update={"max_stop_distance_pct": t.max_stop_distance_pct})
        data = config.data.model_copy(update={"timeframes": sorted(set(config.data.timeframes) | {"1d"})})
        return config.model_copy(update={"exits": exits, "risk": risk, "data": data})
    return config


def paper_bot_supports(config: Config) -> str | None:
    """Why the paper/live bot can't run these settings yet (None = it can)."""
    if config.strategy.mode not in LIVE_READY_MODES:
        return (f"strategy.mode '{config.strategy.mode}' is backtest-only so far. "
                f"Use backtest.bat to test it, or set strategy.mode: day to run the bot.")
    ex = config.execution
    if ex.entry_order not in LIVE_READY_ORDERS or ex.take_profit_order not in LIVE_READY_ORDERS:
        return ("limit orders (execution: entry_order / take_profit_order) are backtest-only so far. "
                "Set both to market to run the bot.")
    return None
