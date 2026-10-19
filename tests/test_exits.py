from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from desk.config import load_config
from desk.exits import check_exit, trailing_stop_price, validate_tighten
from desk.models import Position

T0 = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
EXITS = load_config().exits   # read-only, safe to share across hypothesis examples


def pos(**kw):
    base = dict(coin="BTC", qty=0.01, entry_price=100.0, stop_loss=98.0, take_profit=104.0,
                opened_at=T0, highest_price=100.0)
    base.update(kw)
    return Position(**base)


@pytest.fixture
def cfg(config):
    # trailing starts at +1%, trails 0.8% below the high, max hold 24h
    return config.exits


def test_hold_when_nothing_hit(cfg):
    r = check_exit(pos(), 100.5, T0 + timedelta(hours=1), cfg)
    assert r.action == "HOLD" and r.reason is None
    assert r.highest_price == 100.5 and not r.trailing_active and r.effective_stop == 98.0


def test_stop_loss_hit_exactly_and_below(cfg):
    assert check_exit(pos(), 98.0, T0, cfg).reason == "stop_loss"
    assert check_exit(pos(), 90.0, T0, cfg).reason == "stop_loss"       # gapped through the stop
    assert check_exit(pos(), 98.0001, T0, cfg).action == "HOLD"


def test_take_profit(cfg):
    r = check_exit(pos(), 104.0, T0, cfg)
    assert r.action == "SELL" and r.reason == "take_profit"


def test_trailing_activates_at_threshold(cfg):
    assert not check_exit(pos(), 100.99, T0, cfg).trailing_active
    r = check_exit(pos(), 101.0, T0, cfg)
    assert r.trailing_active
    assert r.effective_stop == pytest.approx(101.0 * 0.992)


def test_trailing_stop_ratchets_up_and_sells_in_profit(cfg):
    p = pos()
    for bid in (101.0, 102.0, 103.0):
        r = check_exit(p, bid, T0, cfg)
        p.highest_price, p.trailing_active = r.highest_price, r.trailing_active
        assert r.action == "HOLD"
    assert p.highest_price == 103.0
    # price falls back: the trailing stop stays at 103 * 0.992 = 102.176
    r = check_exit(p, 102.5, T0, cfg)
    assert r.action == "HOLD" and r.effective_stop == pytest.approx(102.176)
    r = check_exit(p, 102.17, T0, cfg)
    assert r.action == "SELL" and r.reason == "trailing_stop"
    assert r.effective_stop > p.entry_price      # locked in a gain


def test_trailing_never_lowers_the_real_stop(cfg):
    # stop-loss already above where the trailing stop would be
    p = pos(stop_loss=100.9, highest_price=101.0, trailing_active=True)
    r = check_exit(p, 100.95, T0, cfg)
    assert r.effective_stop == 100.9 and r.action == "HOLD"
    r = check_exit(p, 100.9, T0, cfg)
    assert r.reason == "stop_loss"


def test_max_hold(cfg):
    assert check_exit(pos(), 100.0, T0 + timedelta(hours=23, minutes=59), cfg).action == "HOLD"
    r = check_exit(pos(), 100.0, T0 + timedelta(hours=24), cfg)
    assert r.action == "SELL" and r.reason == "max_hold"


def test_stop_beats_max_hold_and_take_profit_beats_max_hold(cfg):
    late = T0 + timedelta(hours=30)
    assert check_exit(pos(), 97.0, late, cfg).reason == "stop_loss"
    assert check_exit(pos(), 105.0, late, cfg).reason == "take_profit"


def test_bad_bid_rejected(cfg):
    with pytest.raises(ValueError):
        check_exit(pos(), 0, T0, cfg)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(min_value=50, max_value=200, allow_nan=False), min_size=1, max_size=40))
def test_effective_stop_never_goes_down(bids):
    cfg = EXITS
    p = pos()
    last_stop = p.stop_loss
    for bid in bids:
        r = check_exit(p, bid, T0, cfg)
        assert r.effective_stop >= last_stop - 1e-12
        assert r.highest_price >= p.highest_price
        if r.action == "SELL":
            break
        p.highest_price, p.trailing_active = r.highest_price, r.trailing_active
        last_stop = r.effective_stop


@settings(max_examples=200, deadline=None)
@given(st.floats(min_value=1, max_value=1000, allow_nan=False))
def test_sell_whenever_bid_at_or_below_stop(bid):
    p = pos(entry_price=500, stop_loss=480, take_profit=600, highest_price=500)
    r = check_exit(p, bid, T0, EXITS)
    if bid <= 480:
        assert r.action == "SELL"


def test_trailing_stop_price(cfg):
    assert trailing_stop_price(200.0, cfg) == pytest.approx(198.4)


# ---- tightening (Exit Agent asks to raise the stop) ---------------------------

def test_tighten_valid(cfg):
    assert validate_tighten(pos(), 99.0, 101.0, cfg) is None


@pytest.mark.parametrize("new_stop,bid,msg", [
    (97.0, 101.0, "only move up"),           # lowering the stop
    (98.0, 101.0, "only move up"),           # same stop
    (100.9, 101.0, "too close"),             # within 0.2% of price
    (102.0, 101.0, "too close"),             # above the price
])
def test_tighten_rejected(cfg, new_stop, bid, msg):
    assert msg in validate_tighten(pos(), new_stop, bid, cfg)
