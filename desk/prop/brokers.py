"""Where prop trades are actually placed.

PropBroker is what the engine talks to. Two versions:
  PaperPropBroker  a simulated Tradeify 247 account kept in the database:
                   live prices, their 0.04% fee, the spread/slippage assumed in
                   the research, the 0.033%/day swap at 22:00 UTC, and stop
                   orders that trigger like the real platform's
  DXtradeBroker    the real account(s) through the DXtrade API. Every entry
                   also places a protective STOP order on the platform, so the
                   position stays protected even if this bot stops running.
"""

from __future__ import annotations

import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from desk.clock import Clock, iso, parse_iso
from desk.data.candles import BinanceProvider
from desk.db import Database
from urllib.parse import quote

from desk.exchange.dxtrade import DXtradeClient, DXtradeError
from desk.prop.rules import PropRules

log = logging.getLogger("desk.prop.brokers")


class BrokerError(Exception):
    pass


@dataclass
class BrokerPosition:
    coin: str
    side: int                    # +1 long, -1 short
    qty: float
    entry_price: float
    mark_price: float
    opened_at: datetime | None = None


@dataclass
class AccountSnapshot:
    account: str
    balance: float
    equity: float
    positions: dict[str, BrokerPosition] = field(default_factory=dict)


@dataclass
class Fill:
    coin: str
    side: int
    qty: float
    price: float | None          # None = not reported (real broker); the engine then uses the mark price
    pnl: float | None = None
    reason: str = ""
    account: str = ""


def round_qty(qty: float, step: float) -> float:
    return math.floor(qty / step + 1e-9) * step if step > 0 else qty


class PriceSource:
    """Live prices (Binance bid/ask midpoints) for the coins, by coin code."""

    def __init__(self, symbols: dict[str, str], http: httpx.Client | None = None):
        self.symbols = symbols
        self.http = http or httpx.Client(timeout=15.0)

    def mids(self) -> dict[str, float]:
        import json
        params = {"symbols": json.dumps(list(self.symbols.values()), separators=(",", ":"))}
        last_error = "no response"
        for host in BinanceProvider.HOSTS:
            try:
                r = self.http.get(host + "/api/v3/ticker/bookTicker", params=params)
                if r.status_code == 200:
                    by_symbol = {row["symbol"]: (float(row["bidPrice"]) + float(row["askPrice"])) / 2
                                 for row in r.json()}
                    return {coin: by_symbol[s] for coin, s in self.symbols.items() if s in by_symbol}
                last_error = f"HTTP {r.status_code}"
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = type(exc).__name__
        raise BrokerError(f"no live prices: {last_error}")


class PropBroker(ABC):
    name = "broker"

    @abstractmethod
    def accounts(self) -> list[str]: ...

    @abstractmethod
    def snapshot(self, account: str) -> AccountSnapshot: ...

    @abstractmethod
    def open(self, account: str, coin: str, side: int, qty: float, stop: float) -> tuple[Fill, str | None]:
        """Market order plus a protective stop. Returns the fill and a reference to the stop order."""

    @abstractmethod
    def close(self, account: str, coin: str, reason: str, stop_ref: str | None = None) -> Fill | None: ...

    @abstractmethod
    def move_stop(self, account: str, coin: str, stop: float, stop_ref: str | None) -> str | None: ...

    def flatten(self, account: str, reason: str, stop_refs: dict[str, str | None] | None = None) -> list[Fill]:
        snap = self.snapshot(account)
        refs = stop_refs or {}
        fills = []
        for coin in list(snap.positions):
            f = self.close(account, coin, reason, refs.get(coin))
            if f:
                fills.append(f)
        return fills

    def qty_step(self, coin: str) -> float | None:
        """The platform's order size step for a coin (None = use config prop.quantity_step)."""
        return None

    def tick(self, now: datetime) -> list[Fill]:
        """Paper only: trigger stop orders. Returns fills of positions that were stopped out."""
        return []

    def daily_swap(self, account: str) -> float:
        """Paper only: charge the overnight fee. The real platform charges it itself."""
        return 0.0


# ---------------------------------------------------------------------------
# Paper
# ---------------------------------------------------------------------------

class PaperPropBroker(PropBroker):
    name = "paper"

    def __init__(self, db: Database, clock: Clock, rules: PropRules, prices: PriceSource, accounts: list[str],
                 cache_seconds: float = 20.0):
        self.cache_seconds = cache_seconds
        self.db = db
        self.clock = clock
        self.rules = rules
        self.prices = prices
        self._accounts = accounts
        self._mids: dict[str, float] = {}
        self._mids_at = 0.0
        for a in accounts:
            if not db.query_one("SELECT 1 FROM paper_prop_accounts WHERE account = ?", (a,)):
                db.execute("INSERT INTO paper_prop_accounts(account, balance) VALUES (?, ?)", (a, rules.account_size))

    def accounts(self) -> list[str]:
        return list(self._accounts)

    def mids(self, max_age: float | None = None) -> dict[str, float]:
        age = self.cache_seconds if max_age is None else min(max_age, self.cache_seconds)
        if not self._mids or time.monotonic() - self._mids_at >= age:
            self._mids = self.prices.mids()
            self._mids_at = time.monotonic()
        return self._mids

    def _balance(self, account: str) -> float:
        return float(self.db.query_one("SELECT balance FROM paper_prop_accounts WHERE account = ?", (account,))[0])

    def _rows(self, account: str) -> list:
        return self.db.query("SELECT * FROM paper_prop_positions WHERE account = ?", (account,))

    def snapshot(self, account: str) -> AccountSnapshot:
        mids = self.mids()
        bal = self._balance(account)
        positions = {}
        upl = 0.0
        for r in self._rows(account):
            mark = mids.get(r["coin"], r["entry_price"])
            positions[r["coin"]] = BrokerPosition(r["coin"], r["side"], r["qty"], r["entry_price"], mark,
                                                  parse_iso(r["opened_at"]))
            upl += r["side"] * r["qty"] * (mark - r["entry_price"])
        return AccountSnapshot(account, bal, bal + upl, positions)

    def open(self, account, coin, side, qty, stop):
        if self.db.query_one("SELECT 1 FROM paper_prop_positions WHERE account = ? AND coin = ?", (account, coin)):
            raise BrokerError(f"{coin} is already open on {account}")
        mid = self.mids(max_age=5.0)[coin]
        px = mid * (1 + side * self.rules.half_cost_pct(coin) / 100)
        fee = qty * px * self.rules.fee_pct / 100
        with self.db.tx() as t:
            t.execute("UPDATE paper_prop_accounts SET balance = balance - ? WHERE account = ?", (fee, account))
            t.execute("INSERT INTO paper_prop_positions(account, coin, side, qty, entry_price, opened_at, stop, fees) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (account, coin, side, qty, px, iso(self.clock.now()), stop, fee))
        return Fill(coin, side, qty, px), f"paper-stop-{coin}"

    def _close_at(self, account: str, row, raw_price: float, reason: str, extra_slip_pct: float = 0.0) -> Fill:
        side = row["side"]
        px = raw_price * (1 - side * (self.rules.half_cost_pct(row["coin"]) + extra_slip_pct) / 100)
        fee = row["qty"] * px * self.rules.fee_pct / 100
        gross = side * row["qty"] * (px - row["entry_price"])
        with self.db.tx() as t:
            t.execute("UPDATE paper_prop_accounts SET balance = balance + ? WHERE account = ?", (gross - fee, account))
            t.execute("DELETE FROM paper_prop_positions WHERE account = ? AND coin = ?", (account, row["coin"]))
        pnl = gross - fee - row["fees"] - row["swap"]
        return Fill(row["coin"], side, row["qty"], px, pnl, reason, account)

    def close(self, account, coin, reason, stop_ref=None):
        row = self.db.query_one("SELECT * FROM paper_prop_positions WHERE account = ? AND coin = ?", (account, coin))
        if row is None:
            return None
        return self._close_at(account, row, self.mids(max_age=5.0)[coin], reason)

    def move_stop(self, account, coin, stop, stop_ref):
        self.db.execute("UPDATE paper_prop_positions SET stop = ? WHERE account = ? AND coin = ?", (stop, account, coin))
        return stop_ref or f"paper-stop-{coin}"

    def tick(self, now):
        mids = self.mids()
        fills = []
        for a in self._accounts:
            for r in self._rows(a):
                mid = mids.get(r["coin"])
                if mid is None:
                    continue
                hit = mid <= r["stop"] if r["side"] > 0 else mid >= r["stop"]
                if hit:
                    # a stop order fills at the market once triggered: at the stop or worse
                    raw = min(mid, r["stop"]) if r["side"] > 0 else max(mid, r["stop"])
                    f = self._close_at(a, r, raw, "stop", self.rules.stop_slippage_pct)
                    fills.append(f)
        return fills

    def daily_swap(self, account):
        mids = self.mids()
        total = 0.0
        for r in self._rows(account):
            sw = r["qty"] * mids.get(r["coin"], r["entry_price"]) * self.rules.swap_pct_per_day / 100
            total += sw
            self.db.execute("UPDATE paper_prop_positions SET swap = swap + ? WHERE account = ? AND coin = ?",
                            (sw, account, r["coin"]))
        if total:
            self.db.execute("UPDATE paper_prop_accounts SET balance = balance - ? WHERE account = ?", (total, account))
        return total


# ---------------------------------------------------------------------------
# DXtrade (real account)
# ---------------------------------------------------------------------------

class DXtradeBroker(PropBroker):
    name = "dxtrade"

    def __init__(self, client: DXtradeClient, symbols: dict[str, str], account_codes: list[str] | None = None,
                 clock: Clock | None = None):
        self.client = client
        self.symbols = symbols
        self.coin_of = {v: k for k, v in symbols.items()}
        self._accounts = account_codes or []
        self.clock = clock or Clock()
        self._steps: dict[str, float] = {}
        self._entries: dict[tuple[str, str], list[tuple[str | None, float]]] = {}   # (positionCode, qty)
        self._if_match_style: str | None = None

    def accounts(self) -> list[str]:
        if not self._accounts:
            self._accounts = self.client.accounts()
            if not self._accounts:
                raise BrokerError("the DXtrade login has no accounts")
        return list(self._accounts)

    def load_instruments(self) -> list[str]:
        """Read each coin's order size step from the platform. Returns coins it doesn't list."""
        missing = []
        for coin, symbol in self.symbols.items():
            try:
                data = self.client.request("GET", f"/instruments/{quote(symbol, safe='')}")
            except DXtradeError:
                missing.append(coin)
                continue
            items = data.get("instruments", [data] if "symbol" in data else [])
            if not items:
                missing.append(coin)
                continue
            step = items[0].get("quantityIncrement") or items[0].get("lotSize")
            if step:
                self._steps[coin] = float(step)
        return missing

    def qty_step(self, coin):
        return self._steps.get(coin)

    @staticmethod
    def _num(d: dict, *keys) -> float | None:
        for k in keys:
            v = d.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    def snapshot(self, account):
        m = self.client.metrics(account)
        # positions: the documented /positions list (several entries per coin are added up)
        agg: dict[str, dict] = {}
        entries: dict[str, list[tuple[str | None, float]]] = {}
        for p in self.client.positions(account):
            coin = self.coin_of.get(p.get("symbol"))
            q = self._num(p, "quantity") or 0.0
            if not coin or q == 0:
                continue
            side = -1 if str(p.get("side", "BUY")).upper() == "SELL" or q < 0 else 1
            a = agg.setdefault(coin, {"side": side, "qty": 0.0, "cost": 0.0, "opened": None})
            a["qty"] += abs(q)
            a["cost"] += abs(q) * (self._num(p, "openPrice", "avgOpenPrice") or 0.0)
            if p.get("openTime") and a["opened"] is None:
                try:
                    a["opened"] = parse_iso(p["openTime"])
                except ValueError:
                    pass
            entries.setdefault(coin, []).append((p.get("positionCode"), abs(q)))
        # mark prices from the metrics' per-position data (field names vary between servers)
        marks: dict[str, float] = {}
        for p in m.get("positions", []) or []:
            coin = self.coin_of.get(p.get("symbol"))
            if not coin or coin not in agg:
                continue
            mark = self._num(p, "markPrice", "currentPrice", "marketPrice", "rate", "price")
            if mark is None:
                pl = self._num(p, "openPl", "openPL", "fpl", "unrealizedPl")
                a = agg[coin]
                if pl is not None and a["qty"]:
                    mark = a["cost"] / a["qty"] + pl / (a["side"] * a["qty"])
            if mark is not None:
                marks[coin] = mark
        positions = {}
        for coin, a in agg.items():
            entry = a["cost"] / a["qty"] if a["qty"] else 0.0
            positions[coin] = BrokerPosition(coin, a["side"], a["qty"], entry, marks.get(coin, entry), a["opened"])
        for coin in self.symbols:
            self._entries[(account, coin)] = entries.get(coin, [])
        balance = self._num(m, "balance") or 0.0
        equity = self._num(m, "equity")
        return AccountSnapshot(account, balance, equity if equity is not None else balance, positions)

    def _code(self, kind: str, coin: str) -> str:
        """A unique order code (the server adds its own prefix)."""
        self._seq = getattr(self, "_seq", 0) + 1
        return f"cd-{kind}-{coin}-{int(time.time() * 1000)}-{self._seq}"[:64]

    @staticmethod
    def _is(order: dict, code: str) -> bool:
        oc = str(order.get("orderCode") or "")
        return order.get("clientOrderId") == code or oc == code or oc.endswith(":" + code)

    def _find_order(self, account: str, code: str | None) -> dict | None:
        if not code:
            return None
        for o in self.client.open_orders(account):
            if self._is(o, code):
                return o
        return None

    def _cancel_order(self, account: str, order: dict) -> None:
        """Cancel with If-Match. The spec doesn't say whether it's the plain version, a quoted
        version or '*', so each is tried once and the one that works is remembered."""
        version = order.get("version")
        styles = {"plain": str(version), "quoted": f'"{version}"', "any": "*"}
        order_code = order.get("orderCode")
        tried = [self._if_match_style] if self._if_match_style else []
        last = None
        for style in tried + [k for k in styles if k not in tried]:
            try:
                self.client.cancel_order(account, order_code, styles[style])
                self._if_match_style = style
                return
            except DXtradeError as exc:
                last = exc
                if exc.status not in (400, 403, 412, 428):
                    raise
        raise last

    def _cancel(self, account: str, code: str | None) -> None:
        o = self._find_order(account, code)
        if o is not None:
            self._cancel_order(account, o)

    def _fill_price(self, account: str, code: str) -> float | None:
        """Average fill price of one of our orders, from the order history."""
        try:
            hist = self.client.request("GET", f"/accounts/{quote(account, safe='')}/orders/history",
                                       params={"limit": 25}).get("orders", [])
        except DXtradeError:
            return None
        for o in hist:
            if self._is(o, code):
                for leg in o.get("legs") or []:
                    if leg.get("averagePrice"):
                        return float(leg["averagePrice"])
                for ex in o.get("executions") or []:
                    if ex.get("averagePrice"):
                        return float(ex["averagePrice"])
        return None

    def _stop_orders(self, account: str, coin: str, stop: float) -> list[str]:
        """One protective STOP per position entry (each must name its positionCode)."""
        refs = []
        for code, qty in self._entries.get((account, coin), []):
            ref = self._code("sl", coin)
            order = {"orderCode": ref, "type": "STOP", "instrument": self.symbols[coin],
                     "positionEffect": "CLOSE", "side": "SELL", "stopPrice": round(stop, 8), "tif": "GTC"}
            if code:
                order["positionCode"] = code       # Tradeify's server: a closing STOP names the position, no quantity
            else:
                order["quantity"] = qty
            self.client.place_order(account, order)
            refs.append(ref)
        return refs

    def open(self, account, coin, side, qty, stop):
        symbol = self.symbols[coin]
        buy, sell = ("BUY", "SELL") if side > 0 else ("SELL", "BUY")
        entry_code = self._code("in", coin)
        self.client.place_order(account, {"orderCode": entry_code, "type": "MARKET", "instrument": symbol,
                                          "quantity": qty, "positionEffect": "OPEN", "side": buy, "tif": "GTC"})
        snap = self.snapshot(account)
        pos = snap.positions.get(coin)
        price = pos.entry_price if pos else self._fill_price(account, entry_code)
        if side < 0:
            raise BrokerError("short positions aren't supported by this broker yet")
        try:
            refs = self._stop_orders(account, coin, stop)
            stop_ref = ",".join(refs) or None
        except DXtradeError as exc:
            log.error("Couldn't place the protective stop for %s on %s: %s", coin, account, exc.message)
            stop_ref = None
        return Fill(coin, side, pos.qty if pos else qty, price), stop_ref

    def _cancel_refs(self, account: str, stop_ref: str | None) -> None:
        for ref in (stop_ref or "").split(","):
            if ref:
                try:
                    self._cancel(account, ref)
                except DXtradeError as exc:
                    log.warning("Couldn't cancel stop order %s: %s", ref, exc.message)

    def close(self, account, coin, reason, stop_ref=None):
        snap = self.snapshot(account)
        pos = snap.positions.get(coin)
        self._cancel_refs(account, stop_ref)
        if pos is None:
            return None
        codes = []
        for code, qty in self._entries.get((account, coin), []) or [(None, pos.qty)]:
            out = self._code("out", coin)
            order = {"orderCode": out, "type": "MARKET", "instrument": self.symbols[coin], "quantity": qty,
                     "positionEffect": "CLOSE", "side": "SELL" if pos.side > 0 else "BUY", "tif": "GTC"}
            if code:
                order["positionCode"] = code                # this platform needs to know which position
            self.client.place_order(account, order)
            codes.append(out)
        price = self._fill_price(account, codes[0]) if codes else None
        pnl = pos.side * pos.qty * (price - pos.entry_price) if price is not None else None
        return Fill(coin, pos.side, pos.qty, price, pnl, reason, account)

    def move_stop(self, account, coin, stop, stop_ref):
        """Cancel and replace the protective stop (the brief gap is a few seconds)."""
        snap = self.snapshot(account)
        if coin not in snap.positions:
            return stop_ref
        self._cancel_refs(account, stop_ref)
        return ",".join(self._stop_orders(account, coin, stop)) or None

    def flatten(self, account, reason, stop_refs=None):
        """Close every position one by one (Tradeify's server has no working /flatten), then
        cancel any orders left over."""
        snap = self.snapshot(account)
        fills = []
        for coin in list(snap.positions):
            try:
                f = self.close(account, coin, reason, (stop_refs or {}).get(coin))
            except DXtradeError as exc:
                log.error("Couldn't close %s on %s: %s", coin, account, exc.message)
                continue
            if f:
                fills.append(f)
        for o in self.client.open_orders(account):            # don't leave stop orders behind
            try:
                self._cancel_order(account, o)
            except DXtradeError as exc:
                log.warning("Couldn't cancel order %s: %s", o.get("orderCode"), exc.message)
        return fills
