import pytest

from desk.broker import BrokerError
from desk.broker.paper import PaperBroker, walk_book
from desk.exchange.coinspot import BookLevel, OrderBook
from tests.fakes import FakeCoinSpot

ASKS = (BookLevel(100.0, 1.0), BookLevel(101.0, 2.0), BookLevel(103.0, 5.0))


def test_walk_book_takes_levels_in_order():
    assert walk_book(ASKS, 0.5) == (0.5, 50.0, 1)
    filled, cost, used = walk_book(ASKS, 2.0)          # 1 @ 100 + 1 @ 101
    assert (filled, cost, used) == (2.0, 201.0, 2)
    filled, cost, used = walk_book(ASKS, 100.0)        # more than the whole book
    assert filled == 8.0 and used == 3


class OneBook:
    def __init__(self, book):
        self.book = book

    def order_book(self, coin):
        return self.book


def broker(config, book):
    return PaperBroker(OneBook(book), config)


def test_buy_walks_real_book_and_adds_slippage_and_fee(config, clock):
    b = broker(config, OrderBook("BTC", (BookLevel(99.0, 10),), ASKS, clock.now()))
    f = b.buy("BTC", 2.0)
    book_avg = 201.0 / 2.0
    assert f.qty == 2.0 and not f.partial
    assert f.avg_price == pytest.approx(book_avg * (1 + config.paper.extra_slippage_pct / 100))
    assert f.notional_aud == pytest.approx(f.qty * f.avg_price)
    assert f.fee_aud == pytest.approx(f.notional_aud * config.fees.market_order_pct / 100)
    assert "2 order-book level(s)" in f.detail


def test_sell_uses_bids_and_slippage_lowers_price(config, clock):
    bids = (BookLevel(99.0, 1.0), BookLevel(98.0, 1.0))
    b = broker(config, OrderBook("BTC", bids, ASKS, clock.now()))
    f = b.sell("BTC", 2.0)
    assert f.avg_price == pytest.approx(98.5 * (1 - config.paper.extra_slippage_pct / 100))


def test_bigger_orders_get_worse_prices(config, clock):
    b = broker(config, OrderBook("BTC", (), ASKS, clock.now()))
    assert b.buy("BTC", 5.0).avg_price > b.buy("BTC", 0.5).avg_price


def test_partial_fill_when_book_too_thin(config, clock):
    b = broker(config, OrderBook("BTC", (), ASKS, clock.now()))
    f = b.buy("BTC", 20.0)
    assert f.qty == 8.0 and f.partial and f.requested_qty == 20.0


def test_empty_book_and_bad_qty(config, clock):
    b = broker(config, OrderBook("BTC", (), (), clock.now()))
    with pytest.raises(BrokerError, match="no sellers"):
        b.buy("BTC", 1.0)
    with pytest.raises(BrokerError, match="no buyers"):
        b.sell("BTC", 1.0)
    with pytest.raises(BrokerError, match="positive"):
        b.buy("BTC", 0)


def test_price_protection_on_buys(config, clock):
    b = broker(config, OrderBook("BTC", (), ASKS, clock.now()))
    with pytest.raises(BrokerError, match="price moved"):
        b.buy("BTC", 5.0, max_price=101.0)


def test_coinspot_outage_becomes_broker_error(config, clock):
    cs = FakeCoinSpot(clock, {"BTC": 100.0})
    cs.down = True
    with pytest.raises(BrokerError, match="order book"):
        PaperBroker(cs, config).buy("BTC", 1.0)
