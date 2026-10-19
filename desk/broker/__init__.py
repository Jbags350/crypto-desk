"""Brokers turn an approved order into a fill: PaperBroker (fake money, Stage 5)
and LiveBroker (real CoinSpot orders, Stage 9). Both return the same Fill."""

from __future__ import annotations

from dataclasses import dataclass


class BrokerError(Exception):
    """The order could not be filled (no liquidity, price moved, exchange error...)."""


@dataclass(frozen=True)
class Fill:
    coin: str
    side: str                # buy | sell
    requested_qty: float
    qty: float               # coins actually bought/sold
    avg_price: float         # AUD per coin, after slippage
    notional_aud: float      # qty x avg_price
    fee_aud: float
    detail: str = ""

    @property
    def partial(self) -> bool:
        return self.qty < self.requested_qty - 1e-12
