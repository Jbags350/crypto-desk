"""CoinSpot market snapshot plus the "is this data safe to trade on?" checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from desk.clock import Clock
from desk.config import CoinConfig
from desk.exchange.coinspot import CoinSpotClient, Quote

log = logging.getLogger("desk.market")


@dataclass(frozen=True)
class MarketSnapshot:
    quotes: dict[str, Quote]        # configured coins that CoinSpot returned
    usdt_aud: Quote | None          # CoinSpot's USDT price in AUD (converts Binance USDT prices)
    fetched_at: datetime
    missing: tuple[str, ...] = ()   # configured coins CoinSpot didn't return

    def age_seconds(self, now: datetime) -> float:
        return (now - self.fetched_at).total_seconds()

    def is_stale(self, now: datetime, max_age_seconds: float) -> bool:
        return self.age_seconds(now) > max_age_seconds


class MarketData:
    """Fetches all configured coins from CoinSpot in ONE request."""

    def __init__(self, client: CoinSpotClient, coins: list[CoinConfig], clock: Clock):
        self.client = client
        self.coins = coins
        self.clock = clock
        self.last: MarketSnapshot | None = None

    def snapshot(self) -> MarketSnapshot:
        quotes = self.client.latest_all()
        wanted = {c.code: quotes[c.code] for c in self.coins if c.code in quotes}
        missing = tuple(c.code for c in self.coins if c.code not in quotes)
        if missing:
            log.warning("CoinSpot returned no price for %s", ", ".join(missing))
        snap = MarketSnapshot(quotes=wanted, usdt_aud=quotes.get("USDT"),
                              fetched_at=self.clock.now(), missing=missing)
        self.last = snap
        return snap


def to_aud(price: float, quote_currency: str, usdt_aud: Quote | None) -> float | None:
    """Convert a global price to AUD. Returns None if it can't."""
    if quote_currency == "AUD":
        return price
    if quote_currency in ("USDT", "USD") and usdt_aud is not None:
        return price * usdt_aud.mid
    return None


def divergence_pct(coinspot_aud: float, global_aud: float) -> float:
    """How far CoinSpot's price is from the global price, in percent (+ = CoinSpot dearer)."""
    if global_aud <= 0:
        raise ValueError("global price must be positive")
    return (coinspot_aud - global_aud) / global_aud * 100
