"""DXtrade API client and the real-account broker, against simulated server responses."""

import json

import httpx
import pytest
import respx

from desk.exchange.dxtrade import DXtradeClient, DXtradeError, parse_timeout
from desk.prop.brokers import DXtradeBroker

BASE = "https://dx.example.com/dxsca-web"


def client(waits=None):
    t = {"now": 1000.0}

    def sleep(s):
        (waits if waits is not None else []).append(s)
        t["now"] += s

    return DXtradeClient(BASE, "jack", "default", "s3cret-pass", http=httpx.Client(), sleep=sleep,
                         monotonic=lambda: t["now"]), t


@pytest.mark.parametrize("value,seconds", [("00:30:00", 1800), ("15:00", 900), (1800, 1800), (1_800_000, 1800),
                                           (None, 1800)])
def test_session_timeout_formats(value, seconds):
    assert parse_timeout(value) == seconds


@respx.mock
def test_login_and_authorised_requests():
    login = respx.post(f"{BASE}/login").mock(return_value=httpx.Response(200, json={"sessionToken": "tok1",
                                                                                     "timeout": "00:30:00"}))
    users = respx.get(f"{BASE}/users/jack%40default").mock(return_value=httpx.Response(200, json={
        "userDetails": [{"accounts": [{"account": "default:111"}, {"account": "default:222"}]}]}))
    c, _ = client()
    assert c.accounts() == ["default:111", "default:222"]
    assert json.loads(login.calls[0].request.content) == {"username": "jack", "domain": "default",
                                                          "password": "s3cret-pass"}
    assert users.calls[0].request.headers["Authorization"] == "DXAPI tok1"


@respx.mock
def test_expired_session_logs_in_again_once():
    respx.post(f"{BASE}/login").mock(side_effect=[httpx.Response(200, json={"sessionToken": "old"}),
                                                  httpx.Response(200, json={"sessionToken": "new"})])
    route = respx.get(f"{BASE}/accounts/default%3A111/positions").mock(side_effect=[
        httpx.Response(401, json={"errorCode": 1, "description": "authorization required"}),
        httpx.Response(200, json={"positions": [{"symbol": "BTC/USD", "quantity": 0.5, "side": "BUY"}]})])
    c, _ = client()
    assert c.positions("default:111")[0]["symbol"] == "BTC/USD"
    assert [call.request.headers["Authorization"] for call in route.calls] == ["DXAPI old", "DXAPI new"]


@respx.mock
def test_errors_never_contain_the_password():
    respx.post(f"{BASE}/login").mock(return_value=httpx.Response(401, json={"errorCode": 3,
                                                                            "description": "Incorrect username or password"}))
    c, _ = client()
    with pytest.raises(DXtradeError) as e:
        c.login()
    assert "Incorrect username or password" in e.value.message and e.value.code == 3
    assert "s3cret-pass" not in str(e.value) and "s3cret-pass" not in repr(c)


@respx.mock
def test_trading_requests_are_spaced_one_per_second():
    respx.post(f"{BASE}/login").mock(return_value=httpx.Response(200, json={"sessionToken": "t"}))
    respx.post(f"{BASE}/accounts/default%3A111/orders").mock(return_value=httpx.Response(200, json={"orderId": 1}))
    waits = []
    c, _ = client(waits)
    c.place_order("default:111", {"orderCode": "a"})
    c.place_order("default:111", {"orderCode": "b"})
    assert waits and waits[-1] == pytest.approx(1.05)


@respx.mock
def test_cancel_sends_if_match():
    respx.post(f"{BASE}/login").mock(return_value=httpx.Response(200, json={"sessionToken": "t"}))
    route = respx.delete(f"{BASE}/accounts/default%3A111/orders/cd-sl-BTC-1").mock(return_value=httpx.Response(200))
    c, _ = client()
    c.cancel_order("default:111", "cd-sl-BTC-1", 7)
    assert route.calls[0].request.headers["If-Match"] == "7"


# ---- the real-account broker -------------------------------------------------------------

PREFIX = "dxsca-integration-session-code:"


class FakeDX:
    """Behaves like Tradeify 247's DXtrade did in the live test trade (10 Oct 2026): every position
    has a positionCode, CLOSE/STOP orders must name it, the server prefixes order codes, and fills
    are reported in the order history."""

    def __init__(self, if_match_ok=lambda tag, version: tag == str(version)):
        self.orders = []
        self.cancelled = []
        self.flattened = []
        self.positions_ = []
        self.open_orders_ = []
        self.history = []
        self.balance = 100_000.0
        self.next_code = 1000
        self.if_match_ok = if_match_ok

    def accounts(self):
        return ["default:111"]

    def metrics(self, account):
        return {"balance": self.balance, "equity": self.balance + 50,
                "positions": [{"positionCode": p["positionCode"], "symbol": p["symbol"], "quantity": p["quantity"],
                               "openPrice": p["openPrice"], "rate": p["openPrice"] * 1.01,
                               "fpl": p["quantity"] * p["openPrice"] * 0.01} for p in self.positions_]}

    def positions(self, account):
        return self.positions_

    def open_orders(self, account):
        return self.open_orders_

    def request(self, method, path, **kw):
        if path.endswith("/orders/history"):
            return {"orders": self.history}
        return {}

    def place_order(self, account, order):
        if order["positionEffect"] == "CLOSE" and "positionCode" not in order:
            raise DXtradeError("POST /orders: HTTP 400: Incorrect request. <positionCode>", 400, 33)
        if order["type"] == "STOP" and order["positionEffect"] == "CLOSE" and "quantity" in order:
            raise DXtradeError("POST /orders: HTTP 400: Incorrect request. Closing STOP/LIMIT orders should not "
                               "have specified quantity", 400, 33)
        self.orders.append(order)
        server_code = PREFIX + order["orderCode"]
        if order["type"] == "MARKET" and order["positionEffect"] == "OPEN":
            self.next_code += 1
            self.positions_.append({"symbol": order["instrument"], "quantity": order["quantity"],
                                    "side": order["side"], "openPrice": 100.0, "positionCode": str(self.next_code),
                                    "openTime": "2026-10-11T00:01:00.000Z"})
            self.history.insert(0, {"orderCode": server_code, "clientOrderId": order["orderCode"],
                                    "legs": [{"averagePrice": 100.0}]})
        if order["type"] == "STOP":
            self.open_orders_.append({"orderCode": server_code, "clientOrderId": order["orderCode"], "version": 3,
                                      "positionCode": order["positionCode"]})
        if order["type"] == "MARKET" and order["positionEffect"] == "CLOSE":
            self.positions_ = [p for p in self.positions_ if p["positionCode"] != order["positionCode"]]
            self.history.insert(0, {"orderCode": server_code, "clientOrderId": order["orderCode"],
                                    "legs": [{"averagePrice": 101.0}]})
        return {"orderId": len(self.orders)}

    def cancel_order(self, account, code, version_tag):
        o = next(o for o in self.open_orders_ if o["orderCode"] == code)
        if not self.if_match_ok(version_tag, o["version"]):
            raise DXtradeError("DELETE /orders: HTTP 412", 412)
        self.cancelled.append((code, version_tag))
        self.open_orders_.remove(o)

    def flatten(self, account, symbol=None):
        raise DXtradeError("POST /flatten: HTTP 404", 404)                    # not available on this server


def test_broker_open_places_market_then_protective_stop():
    dx = FakeDX()
    b = DXtradeBroker(dx, {"BTC": "BTC/USD", "ETH": "ETH/USD"})
    fill, ref = b.open("default:111", "BTC", 1, 0.5, 95.0)
    entry, stop = dx.orders
    assert entry["type"] == "MARKET" and entry["side"] == "BUY" and entry["positionEffect"] == "OPEN"
    assert stop["type"] == "STOP" and stop["side"] == "SELL" and stop["positionEffect"] == "CLOSE"
    assert stop["stopPrice"] == 95.0 and "quantity" not in stop
    assert stop["positionCode"] == dx.positions_[0]["positionCode"]          # the platform requires it
    assert fill.price == 100.0 and ref == stop["orderCode"]


def test_broker_snapshot_maps_symbols_to_coins():
    dx = FakeDX()
    dx.positions_ = [{"symbol": "ETH/USD", "quantity": 2.0, "side": "BUY", "openPrice": 3000.0, "positionCode": "1",
                      "openTime": "2026-10-11T00:01:00.000+00:00"},
                     {"symbol": "EUR/USD", "quantity": 1000, "side": "BUY", "openPrice": 1.1,
                      "positionCode": "2"}]   # not ours
    snap = DXtradeBroker(dx, {"BTC": "BTC/USD", "ETH": "ETH/USD"}).snapshot("default:111")
    assert list(snap.positions) == ["ETH"]
    assert snap.positions["ETH"].qty == 2.0 and snap.positions["ETH"].mark_price == pytest.approx(3030.0)
    assert snap.equity == 100_050


def test_broker_close_cancels_the_stop_then_sells():
    dx = FakeDX()
    b = DXtradeBroker(dx, {"BTC": "BTC/USD"})
    _, ref = b.open("default:111", "BTC", 1, 0.5, 95.0)
    fill = b.close("default:111", "BTC", "rotation", ref)
    assert dx.cancelled == [(PREFIX + ref, "3")]                             # the server's code, plain version
    last = dx.orders[-1]
    assert last["type"] == "MARKET" and last["side"] == "SELL" and last["positionEffect"] == "CLOSE"
    assert dx.positions_ == [] and dx.open_orders_ == []
    assert fill.price == 101.0 and fill.pnl == pytest.approx(0.5)            # read from the order history


def test_broker_flatten_also_cancels_leftover_orders():
    dx = FakeDX()
    b = DXtradeBroker(dx, {"BTC": "BTC/USD"})
    b.open("default:111", "BTC", 1, 0.5, 95.0)
    fills = b.flatten("default:111", "daily_brake")
    assert dx.positions_ == [] and dx.open_orders_ == []                    # closed one by one
    assert [f.coin for f in fills] == ["BTC"] and fills[0].price == 101.0


def test_mark_price_from_open_profit_when_not_given():
    dx = FakeDX()
    dx.positions_ = [{"symbol": "ETH/USD", "quantity": 2.0, "side": "BUY", "openPrice": 3000.0}]
    dx.metrics = lambda account: {"balance": 10_000.0, "equity": 10_100.0,
                                  "positions": [{"symbol": "ETH/USD", "quantity": 2.0, "fpl": 100.0}]}
    snap = DXtradeBroker(dx, {"ETH": "ETH/USD"}).snapshot("default:111")
    assert snap.positions["ETH"].mark_price == pytest.approx(3050.0)


def test_several_entries_for_one_coin_are_added_up():
    dx = FakeDX()
    dx.positions_ = [{"symbol": "ETH/USD", "quantity": 1.0, "side": "BUY", "openPrice": 3000.0, "positionCode": "p1"},
                     {"symbol": "ETH/USD", "quantity": 3.0, "side": "BUY", "openPrice": 3100.0, "positionCode": "p2"}]
    snap = DXtradeBroker(dx, {"ETH": "ETH/USD"}).snapshot("default:111")
    assert snap.positions["ETH"].qty == 4.0 and snap.positions["ETH"].entry_price == pytest.approx(3075.0)


def test_close_names_the_position_when_the_platform_gives_one():
    dx = FakeDX()
    b = DXtradeBroker(dx, {"BTC": "BTC/USD"})
    b.open("default:111", "BTC", 1, 0.5, 95.0)
    code = dx.positions_[0]["positionCode"]
    b.close("default:111", "BTC", "rotation")
    assert dx.orders[-1]["positionCode"] == code


def test_order_steps_come_from_the_platform():
    dx = FakeDX()
    dx.request = lambda method, path, **kw: ({"instruments": [{"symbol": "BTC/USD", "quantityIncrement": 0.01}]}
                                             if "BTC" in path else {"instruments": []})
    b = DXtradeBroker(dx, {"BTC": "BTC/USD", "XYZ": "XYZ/USD"})
    assert b.load_instruments() == ["XYZ"]
    assert b.qty_step("BTC") == 0.01 and b.qty_step("XYZ") is None


def test_mark_price_uses_the_rate_field():
    dx = FakeDX()
    b = DXtradeBroker(dx, {"BTC": "BTC/USD"})
    b.open("default:111", "BTC", 1, 0.5, 95.0)
    assert b.snapshot("default:111").positions["BTC"].mark_price == pytest.approx(101.0)


def test_moving_the_stop_replaces_it_and_names_the_position():
    dx = FakeDX()
    b = DXtradeBroker(dx, {"BTC": "BTC/USD"})
    _, ref = b.open("default:111", "BTC", 1, 0.5, 95.0)
    ref2 = b.move_stop("default:111", "BTC", 97.0, ref)
    assert ref2 != ref and len(dx.open_orders_) == 1
    assert dx.orders[-1]["stopPrice"] == 97.0 and dx.orders[-1]["positionCode"] == dx.positions_[0]["positionCode"]


def test_if_match_form_is_found_and_remembered():
    dx = FakeDX(if_match_ok=lambda tag, version: tag == f'"{version}"')      # this server wants a quoted version
    b = DXtradeBroker(dx, {"BTC": "BTC/USD"})
    _, ref = b.open("default:111", "BTC", 1, 0.5, 95.0)
    ref = b.move_stop("default:111", "BTC", 96.0, ref)
    assert b._if_match_style == "quoted"
    b.close("default:111", "BTC", "test", ref)
    assert [tag for _, tag in dx.cancelled] == ['"3"', '"3"']


def test_flatten_closes_every_coin_without_the_flatten_command():
    dx = FakeDX()
    b = DXtradeBroker(dx, {"BTC": "BTC/USD", "ETH": "ETH/USD"})
    _, r1 = b.open("default:111", "BTC", 1, 0.5, 95.0)
    _, r2 = b.open("default:111", "ETH", 1, 2.0, 95.0)
    fills = b.flatten("default:111", "daily_brake", {"BTC": r1, "ETH": r2})
    assert sorted(f.coin for f in fills) == ["BTC", "ETH"]
    assert dx.positions_ == [] and dx.open_orders_ == []
