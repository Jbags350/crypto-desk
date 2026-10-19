"""Test doubles shared by several test files."""

from __future__ import annotations

import json
from types import SimpleNamespace


def baseline_config():
    """config.yaml pinned to the settings most tests were written for (day mode, 5% per trade),
    so changing your own config.yaml doesn't change what the tests check."""
    from desk.config import DEFAULT_CONFIG_PATH, load_config
    cfg = load_config(DEFAULT_CONFIG_PATH)
    return cfg.model_copy(update={
        "strategy": cfg.strategy.model_copy(update={"mode": "day"}),
        "risk": cfg.risk.model_copy(update={"max_position_pct": 5.0}),
        "execution": cfg.execution.model_copy(update={"entry_order": "market", "take_profit_order": "market"}),
        "prop": cfg.prop.model_copy(update={
            "enabled": False, "broker": "paper", "phase": "1", "paper_accounts": 1, "account_size": 100_000.0,
            "strategy": cfg.prop.strategy.model_copy(update={"top": 3}),
            "risk": cfg.prop.risk.model_copy(update={"risk_pct": 1.0, "scale_up_max": 1.0})}),
    })


def fake_response(payload, *, input_tokens=1000, output_tokens=200, stop_reason="end_turn",
                  thinking=False, cache_read=0, cache_write=0):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    content = []
    if thinking:
        content.append(SimpleNamespace(type="thinking", thinking="", signature="sig"))
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                            cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read)
    return SimpleNamespace(content=content, usage=usage, stop_reason=stop_reason, _request_id="req_test")


class FakeAnthropic:
    """Stands in for anthropic.Anthropic(). Queue responses (or exceptions) in order."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.queue:
            raise AssertionError("FakeAnthropic: no response queued")
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(kwargs)
        return r


# ---------------------------------------------------------------------------
# A fake CoinSpot: prices you can move, and an order book built around them.
# ---------------------------------------------------------------------------

from desk.exchange.coinspot import ApiHealth, BookLevel, CoinSpotUnavailable, OrderBook, Quote, WithdrawalProbe


class FakeCoinSpot:
    def __init__(self, clock, prices: dict[str, float], spread_pct: float = 0.1, depth_aud: float = 1_000_000):
        self.clock = clock
        self.mid = dict(prices)          # coin -> mid price (AUD); change it to move the market
        self.spread_pct = spread_pct
        self.depth_aud = depth_aud       # AUD available at each of 5 book levels per side
        self.health = ApiHealth()
        self.down = False                # True -> every call fails like a CoinSpot outage
        self.has_full_key = False
        self.probe = WithdrawalProbe(False, "withdrawals are not enabled")
        self.book_calls = 0

    def _quote(self, coin, mid):
        half = mid * self.spread_pct / 200
        return Quote(coin, mid - half, mid + half, mid, self.clock.now())

    def latest_all(self):
        if self.down:
            self.health.record_failure(self.clock.now(), "down")
            raise CoinSpotUnavailable("CoinSpot HTTP 503")
        self.health.record_success(self.clock.now())
        return {c: self._quote(c, m) for c, m in self.mid.items()}

    def order_book(self, coin):
        self.book_calls += 1
        if self.down:
            raise CoinSpotUnavailable("CoinSpot HTTP 503")
        q = self._quote(coin.upper(), self.mid[coin.upper()])
        asks = tuple(BookLevel(q.ask * (1 + i * 0.0005), self.depth_aud / q.ask) for i in range(5))
        bids = tuple(BookLevel(q.bid * (1 - i * 0.0005), self.depth_aud / q.bid) for i in range(5))
        return OrderBook(coin.upper(), bids, asks, self.clock.now())

    def withdrawal_probe(self):
        return self.probe
