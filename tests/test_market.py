from datetime import timedelta

import httpx
import pytest
import respx

from desk.data.market import MarketData, divergence_pct, to_aud
from desk.exchange.coinspot import PUBLIC_BASE, CoinSpotClient, Quote


def test_divergence():
    assert divergence_pct(101, 100) == pytest.approx(1.0)
    assert divergence_pct(99, 100) == pytest.approx(-1.0)
    with pytest.raises(ValueError):
        divergence_pct(1, 0)


def test_to_aud(clock):
    usdt = Quote("USDT", 1.43, 1.44, 1.435, clock.now())
    assert to_aud(100, "USDT", usdt) == pytest.approx(143.5)
    assert to_aud(100, "AUD", None) == 100
    assert to_aud(100, "USDT", None) is None


@respx.mock
def test_snapshot_picks_configured_coins_and_reports_missing(clock, config):
    respx.get(f"{PUBLIC_BASE}/latest").mock(return_value=httpx.Response(200, json={"status": "ok", "prices": {
        "btc": {"bid": "1", "ask": "2", "last": "1.5"},
        "eth": {"bid": "1", "ask": "2", "last": "1.5"},
        "usdt": {"bid": "1.43", "ask": "1.44", "last": "1.435"},
        "doge": {"bid": "1", "ask": "2", "last": "1.5"}}}))
    md = MarketData(CoinSpotClient(http=httpx.Client(), clock=clock), config.coins, clock)
    snap = md.snapshot()
    assert set(snap.quotes) == {"BTC", "ETH"}
    assert snap.missing == ("SOL",)
    assert snap.usdt_aud.coin == "USDT"
    assert md.last is snap


@respx.mock
def test_snapshot_staleness(clock, config):
    respx.get(f"{PUBLIC_BASE}/latest").mock(return_value=httpx.Response(200, json={"status": "ok", "prices": {
        "btc": {"bid": "1", "ask": "2", "last": "1.5"}}}))
    snap = MarketData(CoinSpotClient(http=httpx.Client(), clock=clock), config.coins, clock).snapshot()
    assert not snap.is_stale(clock.now(), 120)
    assert snap.is_stale(clock.now() + timedelta(seconds=121), 120)
