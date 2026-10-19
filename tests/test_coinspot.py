import hashlib
import hmac
import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from desk.exchange.coinspot import (
    PRIVATE_BASE,
    PUBLIC_BASE,
    READONLY_BASE,
    CoinSpotAuthError,
    CoinSpotClient,
    CoinSpotError,
    CoinSpotUnavailable,
    NonceSource,
    RateLimiter,
    sign,
)

DESK_DIR = Path(__file__).resolve().parent.parent / "desk"


def make_client(clock, **kw):
    sleeps = []
    c = CoinSpotClient(http=httpx.Client(), clock=clock, sleep=sleeps.append, backoff_seconds=1, **kw)
    c.sleeps = sleeps
    return c


# ---- signing & nonce ---------------------------------------------------

def test_sign_matches_hmac_sha512():
    body = b'{"nonce":123}'
    assert sign("secret", body) == hmac.new(b"secret", body, hashlib.sha512).hexdigest()


def test_nonce_strictly_increases_even_if_clock_goes_backwards():
    times = iter([1000, 1000, 999, 500, 2000])
    n = NonceSource(now_ms=lambda: next(times))
    got = [n.next() for _ in range(5)]
    assert got == [1000, 1001, 1002, 1003, 2000]


def test_nonce_persists_across_restarts():
    store = {}
    a = NonceSource(now_ms=lambda: 5000, save=lambda v: store.__setitem__("n", v))
    a.next(); a.next()
    b = NonceSource(now_ms=lambda: 100, load=lambda: store.get("n"))   # clock reset after restart
    assert b.next() == 5002


@respx.mock
def test_signed_request_sends_key_and_signature_of_exact_body(clock):
    route = respx.post(f"{READONLY_BASE}/status").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    c = make_client(clock, readonly_key="ROKEY", readonly_secret="ROSECRET",
                    nonce=NonceSource(now_ms=lambda: 42))
    assert c.readonly_status() is True
    req = route.calls.last.request
    assert req.headers["key"] == "ROKEY"
    assert req.headers["sign"] == sign("ROSECRET", req.content)
    assert json.loads(req.content) == {"nonce": 42}


@respx.mock
def test_each_retry_gets_a_fresh_nonce(clock):
    route = respx.post(f"{READONLY_BASE}/my/balances").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json={"status": "ok", "balances": []})])
    c = make_client(clock, readonly_key="k", readonly_secret="s")
    c.balances()
    nonces = [json.loads(call.request.content)["nonce"] for call in route.calls]
    assert len(nonces) == 2 and nonces[1] > nonces[0]


# ---- public data -------------------------------------------------------

@respx.mock
def test_latest_all_parses_and_skips_non_aud_markets(clock):
    respx.get(f"{PUBLIC_BASE}/latest").mock(return_value=httpx.Response(200, json={
        "status": "ok", "prices": {
            "btc": {"bid": "118500", "ask": "118539", "last": "118591"},
            "btc_usdt": {"bid": "1", "ask": "1", "last": "1"},
            "usdt": {"bid": "1.43", "ask": "1.44", "last": "1.435"},
            "junk": {"bid": "abc", "ask": "1", "last": "1"},
        }}))
    q = make_client(clock).latest_all()
    assert set(q) == {"BTC", "USDT"}
    assert q["BTC"].bid == 118500 and q["BTC"].ask == 118539
    assert q["BTC"].spread_pct == pytest.approx((118539 - 118500) / 118519.5 * 100)
    assert q["BTC"].fetched_at == clock.now()


@respx.mock
def test_unknown_coin_returns_ok_without_prices_and_is_an_error(clock):
    # Real CoinSpot behaviour: {"status":"ok"} with no prices for an unknown coin
    respx.get(f"{PUBLIC_BASE}/latest/nope").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(CoinSpotError, match="no AUD price"):
        make_client(clock).latest("NOPE")


@respx.mock
def test_order_book_sorted_best_first(clock):
    respx.get(f"{PUBLIC_BASE}/orders/open/btc").mock(return_value=httpx.Response(200, json={
        "status": "ok",
        "buyorders": [{"amount": 1, "rate": 100}, {"amount": 2, "rate": 101}, {"amount": 0, "rate": 99}],
        "sellorders": [{"amount": 1, "rate": 105}, {"amount": 1, "rate": 102}]}))
    book = make_client(clock).order_book("BTC")
    assert [lv.rate for lv in book.bids] == [101, 100]          # zero-amount level dropped
    assert [lv.rate for lv in book.asks] == [102, 105]


# ---- errors, retries, health -------------------------------------------

@respx.mock
def test_retries_server_errors_then_succeeds(clock):
    respx.get(f"{PUBLIC_BASE}/latest/btc").mock(side_effect=[
        httpx.Response(502), httpx.ConnectTimeout("t"),
        httpx.Response(200, json={"status": "ok", "prices": {"bid": "1", "ask": "2", "last": "1.5"}})])
    c = make_client(clock)
    assert c.latest("btc").ask == 2
    assert c.sleeps == [1, 2]                   # exponential backoff
    assert c.health.consecutive_errors == 0


@respx.mock
def test_gives_up_after_max_retries_and_records_failure(clock):
    respx.get(f"{PUBLIC_BASE}/latest").mock(return_value=httpx.Response(503))
    c = make_client(clock)
    with pytest.raises(CoinSpotUnavailable):
        c.latest_all()
    assert len(c.sleeps) == 3
    assert c.health.consecutive_errors == 1
    with pytest.raises(CoinSpotUnavailable):
        c.latest_all()
    assert c.health.consecutive_errors == 2


@respx.mock
def test_bad_request_is_not_retried(clock):
    route = respx.get(f"{PUBLIC_BASE}/orders/open/zzz").mock(
        return_value=httpx.Response(400, json={"status": "error", "message": "Invalid market"}))
    c = make_client(clock)
    with pytest.raises(CoinSpotError, match="Invalid market"):
        c.order_book("zzz")
    assert route.call_count == 1 and c.sleeps == []


@respx.mock
def test_401_raises_auth_error(clock):
    respx.post(f"{READONLY_BASE}/status").mock(
        return_value=httpx.Response(401, json={"status": "error", "message": "invalid key/secret"}))
    c = make_client(clock, readonly_key="k", readonly_secret="s")
    with pytest.raises(CoinSpotAuthError, match="invalid key/secret"):
        c.readonly_status()
    assert c.health.consecutive_errors == 1


def test_signed_call_without_key_fails_cleanly(clock):
    with pytest.raises(CoinSpotAuthError, match="COINSPOT_READONLY_KEY"):
        make_client(clock).balances()


@respx.mock
def test_balances_parses_list_format(clock):
    respx.post(f"{READONLY_BASE}/my/balances").mock(return_value=httpx.Response(200, json={
        "status": "ok", "balances": [{"AUD": {"balance": "100.5", "audbalance": "100.5", "rate": "1"}},
                                     {"btc": {"balance": 0.01, "audbalance": 1185, "rate": 118500}}]}))
    b = make_client(clock, readonly_key="k", readonly_secret="s").balances()
    assert b["AUD"]["balance"] == 100.5
    assert b["BTC"]["rate"] == 118500


# ---- withdrawal probe --------------------------------------------------

@respx.mock
def test_probe_detects_withdrawal_enabled_key(clock):
    respx.post(f"{PRIVATE_BASE}/my/coin/withdraw/senddetails").mock(
        return_value=httpx.Response(200, json={"status": "ok", "networks": []}))
    p = make_client(clock, key="k", secret="s").withdrawal_probe()
    assert p.enabled is True


@respx.mock
def test_probe_ok_when_coinspot_refuses(clock):
    respx.post(f"{PRIVATE_BASE}/my/coin/withdraw/senddetails").mock(
        return_value=httpx.Response(400, json={"status": "error", "message": "permission denied"}))
    p = make_client(clock, key="k", secret="s").withdrawal_probe()
    assert p.enabled is False and "permission denied" in p.detail


@respx.mock
def test_probe_unknown_when_unreachable(clock):
    route = respx.post(f"{PRIVATE_BASE}/my/coin/withdraw/senddetails").mock(side_effect=httpx.ConnectError("x"))
    p = make_client(clock, key="k", secret="s").withdrawal_probe()
    assert p.enabled is None
    assert route.call_count == 1      # probe never retries


def test_probe_requires_full_key(clock):
    with pytest.raises(CoinSpotAuthError):
        make_client(clock, readonly_key="k", readonly_secret="s").withdrawal_probe()


# ---- safety: no money-moving or 1%-fee endpoints anywhere --------------

FORBIDDEN = [
    r"withdraw/send(?!details)",   # sending/withdrawing coins
    r"/my/buy/now", r"/my/sell/now", r"/my/swap", r"/quote/swap",   # instant (1% fee) orders
]


def test_package_contains_no_withdraw_or_instant_endpoints():
    offenders = []
    for path in DESK_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            if re.search(pat, text):
                offenders.append(f"{path.name}: {pat}")
    assert offenders == []


# ---- misc ----------------------------------------------------------------

def test_rate_limiter_waits_when_window_full():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(3, monotonic=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.acquire()
    t[0] = 10.0
    rl.acquire()
    assert slept == [50.0]


def test_repr_hides_keys(clock):
    c = make_client(clock, key="SUPERKEY", secret="SUPERSECRET")
    assert "SUPER" not in repr(c)
