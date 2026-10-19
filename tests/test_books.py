"""Portfolio + Executor: the money maths and crash safety."""

from datetime import timedelta

import pytest

from desk.broker import BrokerError
from desk.broker.paper import PaperBroker
from desk.executor import Executor
from desk.notify import MemoryNotifier
from desk.portfolio import Portfolio
from desk.risk import RiskDecision
from tests.fakes import FakeCoinSpot


@pytest.fixture
def books(config, db, clock):
    cs = FakeCoinSpot(clock, {"BTC": 100_000.0, "ETH": 4_000.0}, spread_pct=0.1)
    pf = Portfolio(db, clock, config)
    note = MemoryNotifier()
    ex = Executor(db, clock, config, PaperBroker(cs, config), pf, note)
    return cs, pf, ex, note


def approved_buy(coin="BTC", qty=0.005, price=100_050.0, stop=99_000.0, tp=102_500.0):
    return RiskDecision(kind="entry", coin=coin, approved=True, checks=(), qty=qty, price=price,
                        notional_aud=qty * price, fee_aud=qty * price * 0.001, stop_loss_aud=stop, take_profit_aud=tp)


def test_starting_balance_is_fixed_on_first_use(books, config, db):
    _, pf, _, _ = books
    assert pf.starting_balance() == config.paper.starting_balance_aud
    db.kv_set("paper_starting_balance_aud", "1234")       # what was saved wins over later config edits
    assert pf.starting_balance() == 1234


def test_buy_records_order_trade_position_and_cash(books, db):
    cs, pf, ex, note = books
    pos = ex.open_position(approved_buy(), cycle_id="c1", debate_id=9)
    assert pos is not None and pos.coin == "BTC" and pos.qty == 0.005
    assert pos.stop_loss == 99_000.0 and pos.take_profit == 102_500.0
    order = db.query_one("SELECT * FROM orders")
    assert order["status"] == "filled" and order["position_id"] == pos.id
    trade = db.query_one("SELECT * FROM trades")
    assert trade["side"] == "buy" and trade["aud_amount"] == pytest.approx(pos.qty * pos.entry_price)
    assert pf.cash_aud() == pytest.approx(10_000 - trade["aud_amount"] - trade["fee_aud"])
    assert note.sent[-1][0] == "trade" and "PAPER BUY" in note.sent[-1][1]
    row = db.query_one("SELECT * FROM positions")
    assert row["cycle_id"] == "c1" and row["debate_id"] == 9


def test_rejected_buy_leaves_no_position_and_no_cash_change(books, db):
    cs, pf, ex, note = books
    cs.down = True
    assert ex.open_position(approved_buy()) is None
    assert pf.open_positions() == [] and pf.cash_aud() == 10_000
    assert db.query_one("SELECT status FROM orders")[0] == "rejected"
    assert note.sent[-1][0] == "order_failed"


def test_unapproved_decision_cannot_be_executed(books):
    _, _, ex, _ = books
    bad = RiskDecision(kind="entry", coin="BTC", approved=False, checks=())
    with pytest.raises(ValueError):
        ex.open_position(bad)


def test_sell_records_cost_base_gain_and_closes(books, db):
    cs, pf, ex, _ = books
    pos = ex.open_position(approved_buy())
    buy = db.query_one("SELECT * FROM trades WHERE side='buy'")
    cs.mid["BTC"] = 102_000.0
    fill = ex.close_position(pos, pos.qty, "take_profit")
    sell = db.query_one("SELECT * FROM trades WHERE side='sell'")
    assert sell["cost_base_aud"] == pytest.approx(buy["aud_amount"] + buy["fee_aud"])
    assert sell["proceeds_aud"] == pytest.approx(fill.notional_aud - fill.fee_aud)
    assert sell["gain_aud"] == pytest.approx(sell["proceeds_aud"] - sell["cost_base_aud"])
    assert sell["gain_aud"] > 0 and sell["acquired_at"] == buy["ts"]
    row = db.query_one("SELECT * FROM positions")
    assert row["status"] == "closed" and row["exit_reason"] == "take_profit"
    assert row["realized_pnl_aud"] == pytest.approx(sell["gain_aud"])
    assert pf.open_positions() == []
    assert pf.cash_aud() == pytest.approx(10_000 + sell["gain_aud"])      # cash = start + realised P&L


def test_partial_sell_splits_cost_base(books, db):
    cs, pf, ex, _ = books
    pos = ex.open_position(approved_buy(qty=0.004))
    ex.close_position(pos, 0.001, "manual_close")
    left = pf.open_positions()[0]
    assert left.qty == pytest.approx(0.003)
    first = db.query_one("SELECT * FROM trades WHERE side='sell'")
    buy = db.query_one("SELECT * FROM trades WHERE side='buy'")
    assert first["cost_base_aud"] == pytest.approx((buy["aud_amount"] + buy["fee_aud"]) / 4)
    ex.close_position(left, left.qty, "manual_close")
    total_cb = db.query_one("SELECT SUM(cost_base_aud) FROM trades WHERE side='sell'")[0]
    assert total_cb == pytest.approx(buy["aud_amount"] + buy["fee_aud"])   # every cent of cost used exactly once
    assert pf.open_positions() == []


def test_closing_twice_is_harmless(books, db):
    cs, pf, ex, _ = books
    pos = ex.open_position(approved_buy())
    assert ex.close_position(pos, pos.qty, "stop_loss") is not None
    assert ex.close_position(pos, pos.qty, "stop_loss") is None          # e.g. fast loop and agent cycle race
    assert db.query_one("SELECT COUNT(*) FROM trades WHERE side='sell'")[0] == 1


def test_failed_sell_keeps_position_and_alerts(books, db):
    cs, pf, ex, note = books
    pos = ex.open_position(approved_buy())
    cs.down = True
    assert ex.close_position(pos, pos.qty, "stop_loss") is None
    assert len(pf.open_positions()) == 1
    assert note.sent[-1][0] == "order_failed" and "Will retry" in note.sent[-1][1]


def test_crash_between_intent_and_fill_is_recovered(books, db):
    cs, pf, ex, _ = books
    ex._new_order("BTC", "buy", 0.01, "entry", 100_000.0, None)      # the bot "died" right after this
    notes = ex.recover()
    assert len(notes) == 1 and "never filled" in notes[0]
    assert db.query_one("SELECT status FROM orders")[0] == "abandoned"
    assert pf.open_positions() == [] and pf.cash_aud() == 10_000
    assert ex.recover() == []                                         # only handled once


def test_equity_values_positions_at_bid(books):
    cs, pf, ex, _ = books
    pos = ex.open_position(approved_buy())
    quotes = cs.latest_all()
    expected = pf.cash_aud() + pos.qty * quotes["BTC"].bid
    assert pf.equity(quotes) == pytest.approx(expected)
    value, missing = pf.positions_value({})
    assert missing == ["BTC"] and value == pytest.approx(pos.qty * pos.entry_price)


def test_day_start_equity_saved_once_per_local_day(books, clock):
    _, pf, _, _ = books
    assert pf.day_start_equity(10_000) == 10_000
    assert pf.day_start_equity(9_000) == 10_000          # same day: first value kept (survives restarts)
    clock.set(clock.next_day_start_utc() + timedelta(minutes=1))
    assert pf.day_start_equity(9_000) == 9_000


def test_day_start_equity_accepts_numpy_numbers(books):
    import numpy as np
    _, pf, _, _ = books
    assert pf.day_start_equity(np.float64(10_004.06)) == pytest.approx(10_004.06)
    assert pf.day_start_equity(1.0) == pytest.approx(10_004.06)       # read back from the database


def test_entries_today_counts_local_day(books, clock):
    _, pf, ex, _ = books
    ex.open_position(approved_buy())
    assert pf.entries_today() == 1
    clock.set(clock.next_day_start_utc() + timedelta(minutes=1))
    assert pf.entries_today() == 0


def test_set_stop_only_moves_up(books):
    _, pf, ex, _ = books
    pos = ex.open_position(approved_buy())
    pf.set_stop(pos.id, 98_000.0)
    assert pf.open_positions()[0].stop_loss == 99_000.0
    pf.set_stop(pos.id, 99_500.0)
    assert pf.open_positions()[0].stop_loss == 99_500.0


def test_state_and_snapshot(books, db):
    cs, pf, ex, _ = books
    ex.open_position(approved_buy())
    quotes = cs.latest_all()
    st = pf.state(quotes)
    assert len(st.open_positions) == 1 and st.entries_today == 1 and st.live_exposure_aud == 0
    pf.snapshot_equity(quotes, st)
    snap = db.query_one("SELECT * FROM equity_snapshots")
    assert snap["equity_aud"] == pytest.approx(st.equity_aud) and snap["open_positions"] == 1
