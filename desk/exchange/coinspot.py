"""CoinSpot API client: public prices, read-only account data, and a key safety probe.

Safety by design:
- This module contains NO code that can withdraw, send, swap or instant-buy.
  (A unit test scans the package to make sure that stays true.)
- Placing market orders (/my/buy, /my/sell) is added in Stage 9 in a
  separate module, and only runs when LIVE_TRADING=true.

API facts (https://www.coinspot.com.au/v2/api):
- Public:    GET  https://www.coinspot.com.au/pubapi/v2/...
- Private:   POST https://www.coinspot.com.au/api/v2/...      (full-access key)
- Read-only: POST https://www.coinspot.com.au/api/v2/ro/...   (read-only key)
- Signed requests: JSON body with an ever-increasing "nonce"; header "key"
  = API key, header "sign" = HMAC-SHA512 of the exact body bytes, using the secret.
- Limit: 1000 requests/minute. Errors: HTTP 401 (bad key), 400 (bad request).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from desk.clock import Clock

log = logging.getLogger("desk.coinspot")

PUBLIC_BASE = "https://www.coinspot.com.au/pubapi/v2"
PRIVATE_BASE = "https://www.coinspot.com.au/api/v2"
READONLY_BASE = PRIVATE_BASE + "/ro"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CoinSpotError(Exception):
    """CoinSpot answered, but with an error (bad coin code, bad request...)."""

    def __init__(self, message: str, *, http_status: int | None = None, endpoint: str | None = None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.endpoint = endpoint


class CoinSpotAuthError(CoinSpotError):
    """The API key/secret was rejected (HTTP 401)."""


class CoinSpotUnavailable(CoinSpotError):
    """Couldn't get an answer: network down, timeout, server error or rate limited."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    coin: str          # upper case, e.g. "BTC"
    bid: float         # highest price someone will buy at (what you get when selling)
    ask: float         # lowest price someone will sell at (what you pay when buying)
    last: float        # last traded price
    fetched_at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid > 0 else float("inf")


@dataclass(frozen=True)
class BookLevel:
    rate: float     # AUD per coin
    amount: float   # coins available at this rate


@dataclass(frozen=True)
class OrderBook:
    coin: str
    bids: tuple[BookLevel, ...]   # buyers, best (highest) first
    asks: tuple[BookLevel, ...]   # sellers, best (lowest) first
    fetched_at: datetime


@dataclass(frozen=True)
class WithdrawalProbe:
    enabled: bool | None   # True = DANGER (key can withdraw), False = safe, None = couldn't tell
    detail: str


# ---------------------------------------------------------------------------
# Plumbing: nonce, signing, rate limit, health
# ---------------------------------------------------------------------------

def sign(secret: str, body: bytes) -> str:
    """HMAC-SHA512 of the exact request body, hex encoded."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()


class NonceSource:
    """Hands out strictly increasing nonces (millisecond clock, never repeats).

    `load`/`save` let the last nonce survive restarts (stored in the database),
    so a restart or a clock that jumps backwards can't reuse an old nonce.
    """

    def __init__(
        self,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        load: Callable[[], int | None] | None = None,
        save: Callable[[int], None] | None = None,
    ):
        self._now_ms = now_ms
        self._save = save
        self._lock = threading.Lock()
        self._last = int(load() or 0) if load else 0

    def next(self) -> int:
        with self._lock:
            n = max(self._now_ms(), self._last + 1)
            self._last = n
            if self._save:
                self._save(n)
            return n


class RateLimiter:
    """At most `max_per_minute` requests in any 60-second window."""

    def __init__(self, max_per_minute: int, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max = max_per_minute
        self._mono = monotonic
        self._sleep = sleep
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._mono()
            while self._times and now - self._times[0] >= 60:
                self._times.popleft()
            if len(self._times) >= self.max:
                wait = 60 - (now - self._times[0])
                if wait > 0:
                    self._sleep(wait)
                self._times.popleft()
            self._times.append(self._mono())


class ApiHealth:
    """Tracks whether CoinSpot is answering. The Risk Manager blocks trades when it isn't."""

    def __init__(self):
        self._lock = threading.Lock()
        self.consecutive_errors = 0
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None

    def record_success(self, when: datetime) -> None:
        with self._lock:
            self.consecutive_errors = 0
            self.last_success = when

    def record_failure(self, when: datetime, message: str) -> None:
        with self._lock:
            self.consecutive_errors += 1
            self.last_error = message
            self.last_error_at = when


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CoinSpotClient:
    def __init__(
        self,
        *,
        readonly_key: str | None = None,
        readonly_secret: str | None = None,
        key: str | None = None,
        secret: str | None = None,
        http: httpx.Client | None = None,
        nonce: NonceSource | None = None,
        clock: Clock | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        max_requests_per_minute: int = 600,
    ):
        self._ro_creds = (readonly_key, readonly_secret) if readonly_key and readonly_secret else None
        self._full_creds = (key, secret) if key and secret else None
        self._http = http or httpx.Client(timeout=timeout, headers={"User-Agent": "crypto-desk/0.1"})
        self._nonce = nonce or NonceSource()
        self._clock = clock or Clock()
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._limiter = RateLimiter(max_requests_per_minute, sleep=sleep)
        self.health = ApiHealth()

    def __repr__(self) -> str:  # never show keys
        return f"CoinSpotClient(readonly_key={'set' if self._ro_creds else 'none'}, full_key={'set' if self._full_creds else 'none'})"

    def close(self) -> None:
        self._http.close()

    @property
    def has_readonly_access(self) -> bool:
        return bool(self._ro_creds or self._full_creds)

    @property
    def has_full_key(self) -> bool:
        return self._full_creds is not None

    # ---- public market data (no key needed) --------------------------
    def latest_all(self) -> dict[str, Quote]:
        """Bid/ask/last for every AUD market, in one request."""
        data = self._request("GET", f"{PUBLIC_BASE}/latest")
        now = self._clock.now()
        quotes: dict[str, Quote] = {}
        for coin, p in (data.get("prices") or {}).items():
            if "_" in coin:          # non-AUD markets look like "btc_usdt"
                continue
            q = _parse_quote(coin, p, now)
            if q:
                quotes[q.coin] = q
        if not quotes:
            raise CoinSpotError("CoinSpot returned no prices", endpoint="/latest")
        return quotes

    def latest(self, coin: str) -> Quote:
        data = self._request("GET", f"{PUBLIC_BASE}/latest/{coin.lower()}")
        q = _parse_quote(coin, data.get("prices"), self._clock.now())
        if q is None:
            # CoinSpot answers {"status":"ok"} with no prices for unknown coins
            raise CoinSpotError(f"CoinSpot has no AUD price for '{coin.upper()}'; check the coin code",
                                endpoint=f"/latest/{coin.lower()}")
        return q

    def order_book(self, coin: str) -> OrderBook:
        """Top open buy and sell orders (CoinSpot returns up to 100 each side)."""
        data = self._request("GET", f"{PUBLIC_BASE}/orders/open/{coin.lower()}")
        bids = sorted(_parse_levels(data.get("buyorders")), key=lambda lv: lv.rate, reverse=True)
        asks = sorted(_parse_levels(data.get("sellorders")), key=lambda lv: lv.rate)
        return OrderBook(coin=coin.upper(), bids=tuple(bids), asks=tuple(asks), fetched_at=self._clock.now())

    def recent_trades(self, coin: str) -> list[dict]:
        """Most recent completed trades on CoinSpot (up to 100 each side)."""
        data = self._request("GET", f"{PUBLIC_BASE}/orders/completed/{coin.lower()}")
        trades = []
        for side, key in (("buy", "buyorders"), ("sell", "sellorders")):
            for o in data.get(key) or []:
                try:
                    trades.append({"side": side, "amount": float(o["amount"]), "rate": float(o["rate"]),
                                   "total": float(o.get("total", 0)), "time": o.get("solddate")})
                except (KeyError, TypeError, ValueError):
                    continue
        return trades

    # ---- read-only account data (read-only key, or full key) ---------
    def readonly_status(self) -> bool:
        self._request("POST", f"{READONLY_BASE}/status", payload={}, creds=self._ro_or_full())
        return True

    def balances(self) -> dict[str, dict[str, float]]:
        """{"BTC": {"balance": coins, "audbalance": AUD value, "rate": AUD price}, ...}"""
        data = self._request("POST", f"{READONLY_BASE}/my/balances", payload={}, creds=self._ro_or_full())
        return _parse_balances(data.get("balances"))

    def my_open_orders(self, coin: str | None = None) -> dict:
        payload = {"cointype": coin.upper()} if coin else {}
        return self._request("POST", f"{READONLY_BASE}/my/orders/market/open", payload=payload,
                             creds=self._ro_or_full())

    def my_completed_orders(self, coin: str | None = None, start: str | None = None,
                            end: str | None = None, limit: int = 200) -> dict:
        payload: dict = {"limit": max(1, min(limit, 500))}
        if coin:
            payload["cointype"] = coin.upper()
        if start:
            payload["startdate"] = start
        if end:
            payload["enddate"] = end
        return self._request("POST", f"{READONLY_BASE}/my/orders/completed", payload=payload,
                             creds=self._ro_or_full())

    # ---- full-access key safety checks -------------------------------
    def full_status(self) -> bool:
        """True if the full-access key is accepted by CoinSpot."""
        self._request("POST", f"{PRIVATE_BASE}/status", payload={}, creds=self._require_full())
        return True

    def withdrawal_probe(self) -> WithdrawalProbe:
        """Find out if the full-access key has withdrawal permission.

        CoinSpot has no "what can this key do?" endpoint. This asks for BTC
        withdrawal *details* (network names and fees). That endpoint moves no
        money, but it only works for keys with withdrawals enabled. So:
          success      -> the key CAN withdraw  -> the bot refuses to run
          error reply  -> the key can't         -> OK
          no reply     -> unknown               -> live trading refuses to start
        """
        creds = self._require_full()
        try:
            self._request("POST", f"{PRIVATE_BASE}/my/coin/withdraw/senddetails",
                          payload={"cointype": "BTC"}, creds=creds, retry=False)
        except CoinSpotUnavailable as exc:
            return WithdrawalProbe(None, f"couldn't reach CoinSpot to check ({exc.message})")
        except CoinSpotError as exc:
            return WithdrawalProbe(False, f"withdrawals are not enabled (CoinSpot replied: '{exc.message}')")
        return WithdrawalProbe(True, "this key CAN withdraw funds. Make a new key with withdrawals switched off")

    # ---- internals ---------------------------------------------------
    def _ro_or_full(self) -> tuple[str, str]:
        creds = self._ro_creds or self._full_creds
        if not creds:
            raise CoinSpotAuthError("No CoinSpot key in .env (COINSPOT_READONLY_KEY / COINSPOT_READONLY_SECRET)")
        return creds

    def _require_full(self) -> tuple[str, str]:
        if not self._full_creds:
            raise CoinSpotAuthError("No full-access CoinSpot key in .env (COINSPOT_KEY / COINSPOT_SECRET)")
        return self._full_creds

    def _request(self, method: str, url: str, *, payload: dict | None = None,
                 creds: tuple[str, str] | None = None, retry: bool = True) -> dict:
        endpoint = url.replace(PRIVATE_BASE, "").replace(PUBLIC_BASE, "")
        attempts = self._max_retries + 1 if retry else 1
        last_error: CoinSpotError | None = None

        for attempt in range(attempts):
            self._limiter.acquire()
            headers: dict[str, str] = {}
            content: bytes | None = None
            if creds is not None:
                body = dict(payload or {})
                body["nonce"] = self._nonce.next()          # fresh nonce + signature on every attempt
                content = json.dumps(body, separators=(",", ":")).encode("utf-8")
                headers = {"Content-Type": "application/json", "key": creds[0], "sign": sign(creds[1], content)}
            try:
                resp = self._http.request(method, url, content=content, headers=headers)
            except httpx.HTTPError as exc:
                last_error = CoinSpotUnavailable(f"{type(exc).__name__} talking to CoinSpot", endpoint=endpoint)
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = CoinSpotUnavailable(f"CoinSpot HTTP {resp.status_code}",
                                                     http_status=resp.status_code, endpoint=endpoint)
                else:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = None
                    message = (data or {}).get("message") if isinstance(data, dict) else None
                    if resp.status_code == 401:
                        err = CoinSpotAuthError(f"CoinSpot rejected the API key: {message or 'HTTP 401'}",
                                                http_status=401, endpoint=endpoint)
                        self.health.record_failure(self._clock.now(), err.message)
                        raise err
                    if not isinstance(data, dict):
                        last_error = CoinSpotUnavailable(f"CoinSpot sent a non-JSON reply (HTTP {resp.status_code})",
                                                         http_status=resp.status_code, endpoint=endpoint)
                    elif resp.status_code != 200 or data.get("status") != "ok":
                        # A real answer from CoinSpot, just an unhappy one. Don't retry.
                        self.health.record_success(self._clock.now())
                        raise CoinSpotError(message or f"CoinSpot error (HTTP {resp.status_code})",
                                            http_status=resp.status_code, endpoint=endpoint)
                    else:
                        self.health.record_success(self._clock.now())
                        return data

            if attempt < attempts - 1:
                delay = self._backoff * (2 ** attempt)
                log.warning("%s on %s; retrying in %.0fs (attempt %d/%d)",
                            last_error.message, endpoint, delay, attempt + 1, attempts)
                self._sleep(delay)

        assert last_error is not None
        self.health.record_failure(self._clock.now(), last_error.message)
        raise last_error


# ---------------------------------------------------------------------------
# Parsing helpers (CoinSpot sends numbers as strings in some places)
# ---------------------------------------------------------------------------

def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _parse_quote(coin: str, p, when: datetime) -> Quote | None:
    if not isinstance(p, dict):
        return None
    bid, ask, last = _num(p.get("bid")), _num(p.get("ask")), _num(p.get("last"))
    if bid is None or ask is None or last is None or bid <= 0 or ask <= 0 or last <= 0:
        return None
    return Quote(coin=coin.upper(), bid=bid, ask=ask, last=last, fetched_at=when)


def _parse_levels(rows) -> list[BookLevel]:
    levels = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rate, amount = _num(r.get("rate")), _num(r.get("amount"))
        if rate and amount and rate > 0 and amount > 0:
            levels.append(BookLevel(rate=rate, amount=amount))
    return levels


def _parse_balances(raw) -> dict[str, dict[str, float]]:
    # CoinSpot sends either [{"BTC": {...}}, {"AUD": {...}}] or {"BTC": {...}}
    items: list[tuple[str, dict]] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                items.extend(entry.items())
    elif isinstance(raw, dict):
        items.extend(raw.items())
    out: dict[str, dict[str, float]] = {}
    for coin, info in items:
        if not isinstance(info, dict):
            continue
        out[str(coin).upper()] = {k: v for k in ("balance", "audbalance", "rate")
                                  if (v := _num(info.get(k))) is not None}
    return out
