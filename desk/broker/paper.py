"""Paper broker: realistic fake fills using CoinSpot's REAL order book.

A buy "eats" the cheapest sell orders on CoinSpot's book until the amount is
filled, exactly like a real market order would, so a big order pays a worse
average price than a small one. Then it adds:
  - paper.extra_slippage_pct (prices move between deciding and filling)
  - the CoinSpot market-order fee (fees.market_order_pct)
No money moves and nothing is sent to CoinSpot except a public price request.
"""

from __future__ import annotations

from desk.broker import BrokerError, Fill
from desk.config import Config
from desk.exchange.coinspot import BookLevel, CoinSpotClient, CoinSpotError
from desk.risk import floor_qty


def walk_book(levels: tuple[BookLevel, ...] | list[BookLevel], qty: float) -> tuple[float, float, int]:
    """Take liquidity level by level. Returns (coins filled, AUD cost before fees, levels used)."""
    filled = cost = 0.0
    used = 0
    for lv in levels:
        if filled >= qty:
            break
        take = min(lv.amount, qty - filled)
        filled += take
        cost += take * lv.rate
        used += 1
    return filled, cost, used


class PaperBroker:
    mode = "paper"

    def __init__(self, coinspot: CoinSpotClient, config: Config):
        self.coinspot = coinspot
        self.fee_rate = config.fees.market_order_pct / 100
        self.extra = config.paper.extra_slippage_pct / 100

    def buy(self, coin: str, qty: float, max_price: float | None = None) -> Fill:
        return self._fill(coin, "buy", qty, max_price)

    def sell(self, coin: str, qty: float) -> Fill:
        return self._fill(coin, "sell", qty, None)

    def _fill(self, coin: str, side: str, qty: float, max_price: float | None) -> Fill:
        if not qty or qty <= 0:
            raise BrokerError("quantity must be positive")
        try:
            book = self.coinspot.order_book(coin)
        except CoinSpotError as exc:
            raise BrokerError(f"couldn't read the CoinSpot order book: {exc.message}") from exc
        levels = book.asks if side == "buy" else book.bids
        if not levels:
            raise BrokerError(f"no {'sellers' if side == 'buy' else 'buyers'} in the CoinSpot order book for {coin}")
        filled, cost, used = walk_book(levels, floor_qty(qty))
        if filled < floor_qty(qty):                      # book thinner than the order: partial fill
            filled, cost, used = walk_book(levels, floor_qty(filled))
        if filled <= 0:
            raise BrokerError("order book too thin to fill anything")
        book_price = cost / filled
        price = book_price * (1 + self.extra) if side == "buy" else book_price * (1 - self.extra)
        if max_price is not None and price > max_price:
            raise BrokerError(f"price moved: would pay {price:.8g}, limit {max_price:.8g}")
        notional = filled * price
        detail = (f"filled from {used} order-book level(s), book average {book_price:.8g}, "
                  f"plus {self.extra * 100:g}% extra slippage")
        return Fill(coin=coin, side=side, requested_qty=qty, qty=filled, avg_price=price,
                    notional_aud=notional, fee_aud=notional * self.fee_rate, detail=detail)
