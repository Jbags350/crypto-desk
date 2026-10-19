"""Core trading records shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Position:
    """An open position the bot bought (and is the only thing it will ever sell)."""

    coin: str
    qty: float                  # coins held
    entry_price: float          # AUD per coin actually paid (after slippage, before fees)
    stop_loss: float            # AUD; sell if the bid falls to this
    take_profit: float          # AUD; sell if the bid rises to this
    opened_at: datetime
    highest_price: float        # highest bid seen since buying (drives the trailing stop)
    entry_fee_aud: float = 0.0
    trailing_active: bool = False
    id: int | None = None
    mode: str = "paper"         # paper | live
    # Per-position exit settings (trend trades use wide, volatility-based stops).
    # None = use the exits: settings in config.yaml.
    trail_pct: float | None = None        # trailing stop distance; when set, it trails from the start
    max_hold_hours: float | None = None
    notes: dict = field(default_factory=dict)

    @property
    def cost_aud(self) -> float:
        return self.qty * self.entry_price + self.entry_fee_aud

    def value_at(self, bid: float) -> float:
        return self.qty * bid

    def unrealized_pct(self, bid: float) -> float:
        """Price change since entry, in % (before fees)."""
        return (bid / self.entry_price - 1) * 100

    def held_hours(self, now: datetime) -> float:
        return (now - self.opened_at).total_seconds() / 3600
