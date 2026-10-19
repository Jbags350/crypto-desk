"""DXtrade REST API client: the platform Tradeify 247 runs on.

Built from Devexperts' DXtrade REST API spec (dx.trade, read Oct 2026):
  POST {base}/login {username, domain, password} -> {sessionToken, timeout}
  header  Authorization: DXAPI <sessionToken>   (session ends after `timeout`
          without activity; a 401 means log in again)
  GET  /users/<username@domain>                 accounts on this login
  GET  /accounts/<acc>/metrics?include-positions=true   balance, equity, positions
  GET  /accounts/<acc>/positions | /orders | /orders/history
  POST /accounts/<acc>/orders                   place (MARKET / STOP ...)
  DELETE /accounts/<acc>/orders/<orderCode>     cancel (needs If-Match)
  POST /accounts/<acc>/flatten                  close everything
Account codes look like "clearing:account" and are URL-encoded in paths.
Default limits: 1 trading request/s and 2 data requests/s per session.

Things the public spec leaves open (the exact If-Match value, how fills are
reported, quantity steps on Tradeify's instruments) are kept in small, clearly
marked spots so they can be checked against a real account (stage R3).
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

import httpx

log = logging.getLogger("desk.dxtrade")

DATA_INTERVAL = 0.55        # seconds between data requests (limit: 2/s)
TRADE_INTERVAL = 1.05       # seconds between trading requests (limit: 1/s)


class DXtradeError(Exception):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def parse_timeout(value) -> float:
    """Session timeout in seconds. The API may send '00:30:00', seconds or milliseconds."""
    if value is None:
        return 1800.0
    if isinstance(value, str) and ":" in value:
        parts = [float(p) for p in value.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        h, m, s = parts[-3:]
        return h * 3600 + m * 60 + s
    v = float(value)
    return v / 1000 if v > 100_000 else v


def enc(part: str) -> str:
    return quote(str(part), safe="")


class DXtradeClient:
    def __init__(self, base_url: str, username: str, domain: str, password: str, *,
                 http: httpx.Client | None = None, sleep=time.sleep, monotonic=time.monotonic):
        if not (base_url and username and password):
            raise DXtradeError("DXTRADE_URL, DXTRADE_USERNAME and DXTRADE_PASSWORD must all be set in .env")
        self.base = base_url.rstrip("/")
        self.username = username
        self.domain = domain or "default"
        self._password = password
        self.http = http or httpx.Client(timeout=20.0)
        self._sleep = sleep
        self._now = monotonic
        self._token: str | None = None
        self._timeout = 1800.0
        self._last_activity = 0.0
        self._last = {"data": 0.0, "trade": 0.0}

    def __repr__(self) -> str:                      # never shows the password or token
        return f"DXtradeClient({self.base}, user={self.username}@{self.domain})"

    # ---- session ---------------------------------------------------------------
    def login(self) -> None:
        try:
            r = self.http.post(self.base + "/login",
                               json={"username": self.username, "domain": self.domain, "password": self._password})
        except httpx.HTTPError as exc:
            raise DXtradeError(f"can't reach the DXtrade server: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise DXtradeError(f"login failed: {self._error_text(r)}", r.status_code, self._error_code(r))
        data = r.json()
        self._token = data.get("sessionToken")
        if not self._token:
            raise DXtradeError("login answered without a session token")
        self._timeout = parse_timeout(data.get("timeout"))
        self._last_activity = self._now()
        log.info("DXtrade: logged in as %s@%s (session timeout %.0f s)", self.username, self.domain, self._timeout)

    def logout(self) -> None:
        if self._token:
            try:
                self.http.post(self.base + "/logout", headers={"Authorization": f"DXAPI {self._token}"})
            except httpx.HTTPError:
                pass
            self._token = None

    def _ensure_session(self) -> None:
        if self._token is None or self._now() - self._last_activity > self._timeout * 0.8:
            self.login()

    def _throttle(self, kind: str) -> None:
        gap = (TRADE_INTERVAL if kind == "trade" else DATA_INTERVAL) - (self._now() - self._last[kind])
        if gap > 0:
            self._sleep(gap)
        self._last[kind] = self._now()

    # ---- requests ----------------------------------------------------------------
    @staticmethod
    def _error_code(r: httpx.Response) -> int | None:
        try:
            d = r.json()
        except ValueError:
            return None
        code = d.get("errorCode", d.get("code", d.get("Code"))) if isinstance(d, dict) else None
        try:
            return int(code) if code is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _error_text(r: httpx.Response) -> str:
        try:
            d = r.json()
            if isinstance(d, dict):
                msg = d.get("description") or d.get("Description") or d.get("message") or d.get("error")
                if msg:
                    return f"HTTP {r.status_code}: {msg}"
        except ValueError:
            pass
        return f"HTTP {r.status_code}"

    def request(self, method: str, path: str, *, kind: str = "data", json=None, params=None,
                if_match: str | None = None, _retry: bool = True):
        self._ensure_session()
        self._throttle(kind)
        headers = {"Authorization": f"DXAPI {self._token}", "Accept": "application/json"}
        if if_match is not None:
            headers["If-Match"] = if_match
        try:
            r = self.http.request(method, self.base + path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise DXtradeError(f"{method} {path.split('?')[0]}: network error {type(exc).__name__}") from exc
        if r.status_code == 401 and _retry:           # session expired: log in again once
            self._token = None
            return self.request(method, path, kind=kind, json=json, params=params, if_match=if_match, _retry=False)
        if r.status_code == 429 and _retry:           # rate limited: wait and try once more
            self._sleep(2.0)
            return self.request(method, path, kind=kind, json=json, params=params, if_match=if_match, _retry=False)
        if r.status_code >= 400:
            raise DXtradeError(f"{method} {path.split('?')[0]}: {self._error_text(r)}", r.status_code,
                               self._error_code(r))
        self._last_activity = self._now()
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    # ---- account data ------------------------------------------------------------
    def accounts(self) -> list[str]:
        data = self.request("GET", f"/users/{enc(self.username + '@' + self.domain)}")
        out = []
        for user in data.get("userDetails", []):
            for acc in user.get("accounts", []):
                if acc.get("account"):
                    out.append(acc["account"])
        return out

    def metrics(self, account: str) -> dict:
        data = self.request("GET", f"/accounts/{enc(account)}/metrics", params={"include-positions": "true"})
        items = data.get("metrics", [])
        if not items:
            raise DXtradeError(f"no metrics returned for account {account}")
        return items[0]

    def positions(self, account: str) -> list[dict]:
        return self.request("GET", f"/accounts/{enc(account)}/positions").get("positions", [])

    def open_orders(self, account: str) -> list[dict]:
        return self.request("GET", f"/accounts/{enc(account)}/orders").get("orders", [])

    def order_history(self, account: str, order_code: str) -> list[dict]:
        return self.request("GET", f"/accounts/{enc(account)}/orders/history",
                            params={"with-client-id": order_code}).get("orders", [])

    # ---- trading ---------------------------------------------------------------
    def place_order(self, account: str, order: dict) -> dict:
        return self.request("POST", f"/accounts/{enc(account)}/orders", kind="trade", json=order)

    def cancel_order(self, account: str, order_code: str, version) -> None:
        # The spec requires If-Match with the order's current version/ETag (exact form: check in R3)
        self.request("DELETE", f"/accounts/{enc(account)}/orders/{enc(order_code)}", kind="trade",
                     if_match=str(version))

    def flatten(self, account: str, symbol: str | None = None) -> None:
        self.request("POST", f"/accounts/{enc(account)}/flatten", kind="trade",
                     json={"symbol": symbol} if symbol else {})
