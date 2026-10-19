"""A prop firm's challenge rules and trading costs, as plain data.

Tradeify 247 numbers come from their help centre (checked 10 Oct 2026):
  - 2-Step: +10% then +5%; 3% daily loss; 6% max loss, static from the
    starting balance; daily limit resets 22:00 UTC and is checked on live
    equity (an open losing trade can breach it)
  - 1-Step: +12% in one phase, same loss limits
  - crypto: 0.04% fee per trade (each side), 0.033% of the position per day
    swap charged at 22:00 UTC (weekends too), leverage 5:1 BTC/ETH, 2:1 others
  - trades must stay open at least 20 seconds; no hedging (long and short of
    the same coin at once, even across accounts); 80% profit split
Spreads aren't published, so they're an assumption here (spread_pct).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PropRules:
    name: str
    account_size: float
    price_usd: float                      # challenge fee
    targets_pct: tuple[float, ...]        # one entry per phase
    daily_loss_pct: float                 # of the starting account size
    max_loss_pct: float                   # static: floor = start * (1 - max_loss_pct)
    daily_reset_hour_utc: int = 22
    profit_split: float = 0.80
    min_hold_seconds: int = 20
    fee_pct: float = 0.04                 # per side, on notional
    swap_pct_per_day: float = 0.033       # on notional, at the daily reset
    leverage: dict[str, float] = field(default_factory=lambda: {"BTC": 5.0, "ETH": 5.0})
    default_leverage: float = 2.0
    spread_pct: dict[str, float] = field(default_factory=lambda: {"BTC": 0.02, "ETH": 0.03})
    default_spread_pct: float = 0.06      # assumption for the other coins
    slippage_pct: float = 0.02            # extra slip on every fill
    stop_slippage_pct: float = 0.05       # extra slip when a stop-loss fills (fast markets)
    trailing_floor: bool = False          # max-loss floor trails the highest CLOSED balance up to the start
    daily_ref: str = "max"                # daily limit measured from: "max" (balance or equity) or "balance"
    consistency_pct: float | None = None  # payouts need best day <= this % of total realized profit
    payout_locks_floor: bool = False      # any payout locks the floor at the starting balance for good

    def lev(self, coin: str) -> float:
        return self.leverage.get(coin, self.default_leverage)

    def half_cost_pct(self, coin: str) -> float:
        """Price disadvantage of one fill (half the spread + slippage), in %."""
        return self.spread_pct.get(coin, self.default_spread_pct) / 2 + self.slippage_pct

    @property
    def daily_loss_usd(self) -> float:
        return self.account_size * self.daily_loss_pct / 100

    @property
    def floor_usd(self) -> float:
        return self.account_size * (1 - self.max_loss_pct / 100)


def tradeify_2step(size: float = 100_000) -> PropRules:
    prices = {10_000: 100, 25_000: 200, 50_000: 350, 100_000: 580}
    return PropRules("Tradeify 247 2-Step", size, prices.get(size, 580), (10.0, 5.0), 3.0, 6.0)


def tradeify_instant(size: float = 10_000) -> PropRules:
    """Instant Funding: no target, funded from day one. 3% daily loss from the previous day's
    closing balance; 6% max loss trailing the highest closed balance until it reaches the
    starting balance; any payout locks the floor at the start; payouts need a 20% consistency
    score (best day / total realized profit); 2:1 leverage on everything."""
    prices = {10_000: 200, 25_000: 340, 50_000: 580, 100_000: 1000}
    return PropRules("Tradeify 247 Instant", size, prices.get(size, 1000), (), 3.0, 6.0, leverage={},
                     default_leverage=2.0, trailing_floor=True, daily_ref="balance", consistency_pct=20.0,
                     payout_locks_floor=True)


def tradeify_1step(size: float = 100_000) -> PropRules:
    prices = {10_000: 120, 25_000: 231, 50_000: 431, 100_000: 838}
    return PropRules("Tradeify 247 1-Step", size, prices.get(size, 838), (12.0,), 3.0, 6.0)


def ftmo_style_2step(size: float = 100_000, spreads: dict[str, float] | None = None) -> PropRules:
    """A typical forex prop 2-Step (FTMO-style): +10% then +5%, 5% daily loss from the day's
    starting balance, 10% max loss fixed from the start, 80% split. Forex costs: ~$3 commission
    per lot per side (0.003%), spreads per pair in %, a small overnight swap. The daily reset is
    the forex day (22:00 UTC), which research data shifts to 00:00."""
    return PropRules("Forex 2-Step (FTMO-style)", size, 590.0, (10.0, 5.0), 5.0, 10.0, daily_reset_hour_utc=0,
                     profit_split=0.80, min_hold_seconds=0, fee_pct=0.003, swap_pct_per_day=0.005,
                     leverage={}, default_leverage=30.0, spread_pct=dict(spreads or {}),
                     default_spread_pct=0.015, slippage_pct=0.003, stop_slippage_pct=0.01, daily_ref="balance")
