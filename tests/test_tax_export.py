import csv
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from desk.clock import iso
from desk.tax_export import COLUMNS, export, fy_bounds

SYD = ZoneInfo("Australia/Sydney")


def add_trade(db, ts, side, mode="live", coin="BTC", qty=0.01, price=100_000.0, fee=1.0, gain=None,
              cost_base=None, acquired=None, pid=1):
    db.execute("INSERT INTO trades(ts, mode, coin, side, qty, price, aud_amount, fee_aud, order_id, position_id, "
               "reason, cost_base_aud, proceeds_aud, gain_aud, acquired_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (iso(ts), mode, coin, side, qty, price, qty * price, fee, 1, pid, "test", cost_base,
                (qty * price - fee) if side == "sell" else None, gain, iso(acquired) if acquired else None))


def read(path):
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_fy_bounds_are_local_australian_dates():
    start, end = fy_bounds(2026, SYD)
    assert start == datetime(2025, 7, 1, tzinfo=SYD) and end == datetime(2026, 7, 1, tzinfo=SYD)
    assert start.astimezone(UTC) == datetime(2025, 6, 30, 14, 0, tzinfo=UTC)    # AEST is UTC+10 in winter


def test_export_rows_columns_and_totals(db, tmp_path):
    bought = datetime(2026, 3, 1, 1, 0, tzinfo=UTC)
    sold = datetime(2026, 3, 3, 1, 0, tzinfo=UTC)
    add_trade(db, bought, "buy")
    add_trade(db, sold, "sell", price=102_000.0, cost_base=1001.0, gain=17.98, acquired=bought)
    add_trade(db, sold, "sell", coin="ETH", qty=0.1, price=4000.0, cost_base=410.0, gain=-11.0,
              acquired=bought, pid=2)
    out = tmp_path / "tax.csv"
    t = export(db, SYD, 2026, out)
    rows = read(out)
    assert list(rows[0].keys()) == COLUMNS
    assert [r["side"] for r in rows] == ["BUY", "SELL", "SELL"]
    buy, sell = rows[0], rows[1]
    assert buy["net_aud"] == "1001.00" and buy["cost_base_aud"] == "" and buy["capital_gain_aud"] == ""
    assert sell["net_aud"] == f"{0.01 * 102_000 - 1:.2f}" and sell["capital_gain_aud"] == "17.98"
    assert sell["days_held"] == "2" and sell["acquired_date_local"] == "2026-03-01"
    assert sell["date_local"] == "2026-03-03 12:00:00"                 # AEDT = UTC+11 in March
    assert t["gains"] == pytest.approx(17.98) and t["losses"] == pytest.approx(-11.0)
    assert t == {**t, "trades": 3, "buys": 1, "sells": 2}


def test_financial_year_boundaries_use_local_time(db, tmp_path):
    # 30 June 2026 23:30 Sydney = 13:30 UTC (still FY2026); 1 July 00:30 Sydney (FY2027)
    add_trade(db, datetime(2026, 6, 30, 13, 30, tzinfo=UTC), "buy")
    add_trade(db, datetime(2026, 6, 30, 14, 30, tzinfo=UTC), "buy")
    assert export(db, SYD, 2026, tmp_path / "a.csv")["trades"] == 1
    assert export(db, SYD, 2027, tmp_path / "b.csv")["trades"] == 1


def test_paper_trades_excluded_unless_asked(db, tmp_path):
    add_trade(db, datetime(2026, 3, 1, tzinfo=UTC), "buy", mode="paper")
    assert export(db, SYD, 2026, tmp_path / "a.csv")["trades"] == 0
    assert export(db, SYD, 2026, tmp_path / "b.csv", include_paper=True)["trades"] == 1
