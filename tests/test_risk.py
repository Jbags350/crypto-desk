"""Risk Manager tests. This is the module that protects real money, so it is
tested from every angle: each rule alone, exact boundaries on both sides,
sizing limits, combinations, garbage input, and randomised property tests
that hunt for any way to get a bad trade approved."""

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from desk.clock import FakeClock
from tests.fakes import baseline_config
from desk.models import Position
from desk.risk import (
    PROTECTIVE_EXIT_REASONS,
    Controls,
    EntryRequest,
    MarketState,
    PortfolioState,
    RiskManager,
    daily_pause_active,
    floor_qty,
    record_decision,
)

CFG = baseline_config()        # config.yaml pinned to day mode, 5% per trade (read-only; safe to share)
RM = RiskManager(CFG)
T0 = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)

# A trade that passes every rule with the default config:
#   ask 100,000, bid 99,900 (spread ~0.10%) -> round trip ~0.40% -> needs gain >= ~1.20%
#   stop -1% (99,000), target +2% (102,000) -> reward:risk 2.0
GOOD_REQ = EntryRequest("BTC", 100_000.0, 99_000.0, 102_000.0, 65, "BUY")
GOOD_PF = PortfolioState(equity_aud=10_000.0, cash_aud=10_000.0, day_start_equity_aud=10_000.0)
GOOD_MKT = MarketState("BTC", bid=99_900.0, ask=100_000.0, quote_age_seconds=5)
GOOD_CTL = Controls()


def pos(coin="ETH", qty=0.1, entry=3000.0):
    return Position(coin, qty, entry, entry * 0.98, entry * 1.04, T0, entry)


def entry(req=GOOD_REQ, pf=GOOD_PF, mkt=GOOD_MKT, ctl=GOOD_CTL, rm=RM):
    return rm.check_entry(req, pf, mkt, ctl)


def failed(d):
    return {c.name for c in d.failures}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_good_trade_is_approved_and_sized_to_5_percent():
    d = entry()
    assert d.approved, d.reasons
    assert d.failures == []
    assert d.price == 100_000.0
    # sized at the worst accepted price (ask + 0.5%): floor(500 / 1.001 / 100,500, 8dp)
    assert d.qty == pytest.approx(0.00497015, abs=1e-8)
    assert d.max_price == pytest.approx(100_500.0)
    assert d.qty * d.max_price * 1.001 <= 500.0 + 1e-9         # even the worst fill stays within 5%
    assert d.notional_aud + d.fee_aud <= 500.0 + 1e-9         # never more than 5% of 10,000 incl. fee
    assert d.notional_aud == pytest.approx(497.015)
    assert d.fee_aud == pytest.approx(0.497015)
    assert d.stop_loss_aud == 99_000 and d.take_profit_aud == 102_000
    assert d.metrics["size_limited_by"] == "5.0% of portfolio"
    assert d.metrics["reward_risk"] == pytest.approx(2.0)
    assert not d.daily_limit_hit
    assert "APPROVED entry BTC" in d.summary()


def test_every_check_is_reported_even_when_approved():
    names = [c.name for c in entry().checks]
    for expected in ("stop_loss_present", "take_profit_present", "kill_switch", "not_paused", "daily_loss_limit", "max_entries_per_day", "max_open_positions",
                     "one_position_per_coin", "fresh_price", "data_healthy", "api_healthy", "no_major_event",
                     "skeptic_agreed", "min_confidence", "entry_near_market", "stop_loss_below_entry",
                     "stop_distance", "take_profit_above_entry", "reward_risk", "beats_costs", "position_size"):
        assert expected in names


# ---------------------------------------------------------------------------
# Each rule on its own: change ONE thing, exactly the expected checks fail
# ---------------------------------------------------------------------------

SINGLE_FAILURES = [
    ("kill switch on", dict(ctl=Controls(killed=True)), {"kill_switch"}),
    ("paused", dict(ctl=Controls(paused=True)), {"not_paused"}),
    ("daily limit already hit today", dict(ctl=Controls(daily_loss_paused=True)), {"daily_loss_limit"}),
    ("down 3% today", dict(pf=replace(GOOD_PF, equity_aud=9_700.0, cash_aud=9_700.0)), {"daily_loss_limit"}),
    ("6 trades already today", dict(pf=replace(GOOD_PF, entries_today=6)), {"max_entries_per_day"}),
    ("3 positions open", dict(pf=replace(GOOD_PF, open_positions=(pos("ETH"), pos("SOL"), pos("XRP")))),
     {"max_open_positions"}),
    ("already holding BTC", dict(pf=replace(GOOD_PF, open_positions=(pos("BTC", 0.001, 100_000),))),
     {"one_position_per_coin"}),
    ("stale price", dict(mkt=replace(GOOD_MKT, quote_age_seconds=121)), {"fresh_price"}),
    ("scanner data problem", dict(mkt=replace(GOOD_MKT, data_problems=("15m candles are stale",))),
     {"data_healthy"}),
    ("CoinSpot erroring", dict(mkt=replace(GOOD_MKT, api_error_streak=3)), {"api_healthy"}),
    ("major event", dict(ctl=Controls(event_blocks=("exchange hack reported",))), {"no_major_event"}),
    ("skeptic WAIT", dict(req=replace(GOOD_REQ, skeptic_vote="WAIT")), {"skeptic_agreed"}),
    ("skeptic SKIP", dict(req=replace(GOOD_REQ, skeptic_vote="SKIP")), {"skeptic_agreed"}),
    ("skeptic SELL on a buy", dict(req=replace(GOOD_REQ, skeptic_vote="SELL")), {"skeptic_agreed"}),
    ("no skeptic vote", dict(req=replace(GOOD_REQ, skeptic_vote=None)), {"skeptic_agreed"}),
    ("low confidence", dict(req=replace(GOOD_REQ, confidence=59)), {"min_confidence"}),
    ("entry far from market", dict(req=replace(GOOD_REQ, entry_aud=100_600.0)), {"entry_near_market"}),
    ("stop too far", dict(req=replace(GOOD_REQ, stop_loss_aud=94_900.0, take_profit_aud=108_000.0)),
     {"stop_distance"}),
    ("reward:risk too low", dict(req=replace(GOOD_REQ, take_profit_aud=101_400.0)), {"reward_risk"}),
    ("gain doesn't beat costs", dict(req=replace(GOOD_REQ, stop_loss_aud=99_500.0, take_profit_aud=101_000.0)),
     {"beats_costs"}),
    ("not enough cash", dict(pf=replace(GOOD_PF, cash_aud=5.0)), {"position_size"}),
    ("unknown coin", dict(req=replace(GOOD_REQ, coin="DOGE"), mkt=replace(GOOD_MKT, coin="DOGE")), {"known_coin"}),
    ("market data for another coin", dict(mkt=replace(GOOD_MKT, coin="ETH")), {"known_coin"}),
]


@pytest.mark.parametrize("label,changes,expected", SINGLE_FAILURES, ids=[s[0] for s in SINGLE_FAILURES])
def test_single_rule_failures(label, changes, expected):
    d = entry(**changes)
    assert not d.approved
    assert failed(d) == expected, d.reasons
    assert d.qty == 0 and d.notional_aud == 0 and d.fee_aud == 0      # a rejection never carries a size


def test_stop_at_or_above_entry():
    for stop in (100_000.0, 100_500.0):
        d = entry(req=replace(GOOD_REQ, stop_loss_aud=stop))
        assert not d.approved
        assert {"stop_loss_below_entry", "stop_distance", "reward_risk"} <= failed(d)


@pytest.mark.parametrize("stop", [0.0, -5.0, None, float("nan")])
def test_missing_stop_loss_is_named_clearly(stop):
    d = entry(req=replace(GOOD_REQ, stop_loss_aud=stop))
    assert not d.approved
    assert "stop_loss_present" in failed(d)
    assert "NO STOP-LOSS" in " ".join(d.reasons)


def test_missing_take_profit_is_named_clearly():
    assert "take_profit_present" in failed(entry(req=replace(GOOD_REQ, take_profit_aud=0.0)))


def test_take_profit_at_or_below_entry():
    d = entry(req=replace(GOOD_REQ, take_profit_aud=99_999.0))
    assert {"take_profit_above_entry", "reward_risk", "beats_costs"} <= failed(d)


def test_all_failures_are_reported_together():
    d = entry(req=replace(GOOD_REQ, confidence=10, skeptic_vote="SKIP"), ctl=Controls(killed=True, paused=True))
    assert failed(d) == {"kill_switch", "not_paused", "min_confidence", "skeptic_agreed"}
    assert len(d.reasons) == 4 and d.summary().startswith("REJECTED")


# ---------------------------------------------------------------------------
# Exact boundaries: the limit itself vs a hair past it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("equity,ok", [(9_701.0, True), (9_700.0, False), (9_699.0, False), (10_500.0, True)])
def test_daily_loss_boundary(equity, ok):
    d = entry(pf=replace(GOOD_PF, equity_aud=equity, cash_aud=equity))
    assert d.approved is ok, d.reasons


@pytest.mark.parametrize("n,ok", [(0, True), (5, True), (6, False), (7, False)])
def test_entries_per_day_boundary(n, ok):
    assert entry(pf=replace(GOOD_PF, entries_today=n)).approved is ok


@pytest.mark.parametrize("held,ok", [((), True), ((pos("ETH"),), True), ((pos("ETH"), pos("SOL")), True),
                                     ((pos("ETH"), pos("SOL"), pos("XRP")), False)])
def test_open_positions_boundary(held, ok):
    assert entry(pf=replace(GOOD_PF, open_positions=held)).approved is ok


@pytest.mark.parametrize("conf,ok", [(100, True), (60, True), (59.99, False), (59, False), (0, False)])
def test_confidence_boundary(conf, ok):
    assert entry(req=replace(GOOD_REQ, confidence=conf)).approved is ok


@pytest.mark.parametrize("age,ok", [(0, True), (120, True), (120.01, False), (-1, False)])
def test_price_age_boundary(age, ok):
    assert entry(mkt=replace(GOOD_MKT, quote_age_seconds=age)).approved is ok


@pytest.mark.parametrize("streak,ok", [(0, True), (2, True), (3, False)])
def test_api_error_streak_boundary(streak, ok):
    assert entry(mkt=replace(GOOD_MKT, api_error_streak=streak)).approved is ok


@pytest.mark.parametrize("stop,tp,ok", [
    (95_000.0, 107_500.0, True),          # exactly 5% stop, reward:risk exactly 1.5
    (94_999.0, 107_600.0, False),         # 5.001% stop
])
def test_stop_distance_boundary(stop, tp, ok):
    assert entry(req=replace(GOOD_REQ, stop_loss_aud=stop, take_profit_aud=tp)).approved is ok


@pytest.mark.parametrize("tp,ok", [(101_500.0, True), (101_499.0, False)])   # stop -1%: R:R exactly 1.5 vs 1.499
def test_reward_risk_boundary(tp, ok):
    assert entry(req=replace(GOOD_REQ, take_profit_aud=tp)).approved is ok


def test_cost_rule_boundary():
    spread = (100_000 - 99_900) / 99_950 * 100
    need = 3.0 * (0.2 + spread + 0.1)                          # min_edge_multiple x round-trip cost
    exact = 100_000 * (1 + need / 100)
    ok = entry(req=replace(GOOD_REQ, stop_loss_aud=99_500.0, take_profit_aud=exact))
    assert ok.approved, ok.reasons
    assert ok.metrics["round_trip_cost_pct"] == pytest.approx(0.2 + spread + 0.1)
    bad = entry(req=replace(GOOD_REQ, stop_loss_aud=99_500.0, take_profit_aud=exact - 1))
    assert failed(bad) == {"beats_costs"}


def test_wider_spread_makes_the_cost_rule_stricter():
    wide = replace(GOOD_MKT, bid=99_000.0)                     # ~1% spread -> needs ~3.9% gain
    assert failed(entry(mkt=wide)) == {"beats_costs"}


@pytest.mark.parametrize("e,ok", [(100_500.0, True), (99_500.0, True), (100_501.0, False), (99_499.0, False)])
def test_entry_gap_boundary(e, ok):
    assert entry(req=replace(GOOD_REQ, entry_aud=e)).approved is ok


@pytest.mark.parametrize("cash,ok", [(10.07, True), (10.05, False)])
def test_minimum_order_boundary(cash, ok):
    # sized at ask+0.5%: 10.07 cash -> 0.00010009 BTC = A$10.009 >= A$10 ; 10.05 cash -> A$9.99 < A$10
    assert entry(pf=replace(GOOD_PF, cash_aud=cash)).approved is ok


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def test_size_limited_by_cash():
    d = entry(pf=replace(GOOD_PF, cash_aud=300.0))
    assert d.approved and d.metrics["size_limited_by"] == "available cash"
    assert d.notional_aud + d.fee_aud <= 300.0 + 1e-9


def test_size_scales_with_equity():
    d = entry(pf=PortfolioState(equity_aud=50_000.0, cash_aud=50_000.0, day_start_equity_aud=50_000.0))
    assert d.notional_aud + d.fee_aud == pytest.approx(2_500.0 / 1.005, abs=0.01)


def test_qty_has_at_most_8_decimals():
    d = entry(mkt=replace(GOOD_MKT, ask=3_123.456789, bid=3_120.0),
              req=EntryRequest("BTC", 3_123.456789, 3_090.0, 3_200.0, 70, "BUY"))
    assert d.approved, d.reasons
    assert abs(d.qty * 1e8 - round(d.qty * 1e8)) < 1e-6


def test_floor_qty():
    assert floor_qty(0.123456789) == 0.12345678
    assert floor_qty(0) == 0 and floor_qty(-1) == 0 and floor_qty(float("nan")) == 0


def test_live_capital_cap_limits_size():
    ctl = Controls(live=True, live_capital_cap_aud=200.0)
    d = entry(ctl=ctl)
    assert d.approved and d.metrics["size_limited_by"] == "live capital cap"
    assert d.notional_aud + d.fee_aud <= 200.0 + 1e-9


def test_live_cap_counts_existing_exposure():
    ctl = Controls(live=True, live_capital_cap_aud=200.0)
    d = entry(ctl=ctl, pf=replace(GOOD_PF, live_exposure_aud=150.0))
    assert d.approved and d.notional_aud + d.fee_aud <= 50.0 + 1e-9
    full = entry(ctl=ctl, pf=replace(GOOD_PF, live_exposure_aud=200.0))
    assert failed(full) == {"position_size"}


def test_live_without_a_cap_is_refused():
    d = entry(ctl=Controls(live=True, live_capital_cap_aud=None))
    assert failed(d) == {"position_size"} and "max_capital_aud" in d.reasons[0]


def test_paper_mode_ignores_live_cap():
    assert entry(ctl=Controls(live=False, live_capital_cap_aud=1.0)).approved


# ---------------------------------------------------------------------------
# Garbage input never crashes and never approves
# ---------------------------------------------------------------------------

NAN, INF = float("nan"), float("inf")
GARBAGE = [
    dict(req=replace(GOOD_REQ, entry_aud=NAN)),
    dict(req=replace(GOOD_REQ, stop_loss_aud=-1.0)),
    dict(req=replace(GOOD_REQ, stop_loss_aud=0.0)),
    dict(req=replace(GOOD_REQ, take_profit_aud=INF)),
    dict(req=replace(GOOD_REQ, confidence=NAN)),
    dict(req=replace(GOOD_REQ, stop_loss_aud=None)),
    dict(mkt=replace(GOOD_MKT, ask=0.0)),
    dict(mkt=replace(GOOD_MKT, bid=NAN)),
    dict(mkt=replace(GOOD_MKT, bid=100_100.0)),                  # crossed market: bid above ask
    dict(mkt=replace(GOOD_MKT, quote_age_seconds=NAN)),
    dict(pf=replace(GOOD_PF, equity_aud=0.0)),
    dict(pf=replace(GOOD_PF, day_start_equity_aud=0.0)),
    dict(pf=replace(GOOD_PF, cash_aud=-50.0)),
    dict(pf=replace(GOOD_PF, equity_aud=NAN)),
]


@pytest.mark.parametrize("changes", GARBAGE)
def test_garbage_is_rejected_without_crashing(changes):
    d = entry(**changes)
    assert not d.approved
    assert d.qty == 0 and d.notional_aud == 0


@pytest.mark.parametrize("changes", [dict(pf=replace(GOOD_PF, equity_aud=NAN)),
                                     dict(pf=replace(GOOD_PF, day_start_equity_aud=0.0))])
def test_unknown_pnl_blocks_the_trade_but_does_not_pause_the_whole_day(changes):
    d = entry(**changes)
    assert not d.approved and not d.daily_limit_hit


# ---------------------------------------------------------------------------
# Daily loss limit: flag and the local-midnight reset
# ---------------------------------------------------------------------------

def test_daily_limit_hit_flag_only_when_newly_hit():
    down = replace(GOOD_PF, equity_aud=9_650.0, cash_aud=9_650.0)
    assert entry(pf=down).daily_limit_hit is True
    assert entry(pf=down, ctl=Controls(daily_loss_paused=True)).daily_limit_hit is False
    assert entry().daily_limit_hit is False
    assert RM.daily_loss_hit(down) and not RM.daily_loss_hit(GOOD_PF)


def test_unrealised_losses_count_towards_daily_limit():
    # cash unchanged, but an open position has fallen: equity is what counts
    pf = PortfolioState(equity_aud=9_690.0, cash_aud=9_000.0, day_start_equity_aud=10_000.0,
                        open_positions=(pos("ETH", 0.23, 3000.0),))
    assert "daily_loss_limit" in failed(entry(pf=pf))


def test_daily_pause_lasts_until_local_midnight_sydney():
    # Sydney is UTC+11 in October: local midnight = 13:00 UTC
    clock = FakeClock(datetime(2026, 10, 9, 12, 50, tzinfo=UTC), tz="Australia/Sydney")   # 23:50 local
    paused_on = clock.local_date()
    assert daily_pause_active(paused_on, clock.local_date())
    clock.advance(minutes=9)                                     # 23:59 local, still the same day
    assert daily_pause_active(paused_on, clock.local_date())
    clock.advance(minutes=2)                                     # 00:01 local, next day
    assert not daily_pause_active(paused_on, clock.local_date())
    assert not daily_pause_active(None, clock.local_date())


def test_daily_pause_uses_local_not_utc_day():
    # 10:00 UTC and 14:00 UTC are the same UTC day but different Sydney days
    clock = FakeClock(datetime(2026, 10, 9, 10, 0, tzinfo=UTC), tz="Australia/Sydney")
    paused_on = clock.local_date()
    clock.set(datetime(2026, 10, 9, 14, 0, tzinfo=UTC))
    assert not daily_pause_active(paused_on, clock.local_date())


# ---------------------------------------------------------------------------
# Limits really come from config.yaml
# ---------------------------------------------------------------------------

def rm_with(**risk_changes):
    return RiskManager(CFG.model_copy(update={"risk": CFG.risk.model_copy(update=risk_changes)}))


def test_config_changes_are_respected():
    assert not entry(rm=rm_with(max_open_positions=1), pf=replace(GOOD_PF, open_positions=(pos(),))).approved
    assert entry(rm=rm_with(min_confidence=0), req=replace(GOOD_REQ, confidence=0)).approved
    d = entry(rm=rm_with(max_position_pct=10.0))
    assert d.notional_aud + d.fee_aud == pytest.approx(1_000.0 / 1.005, abs=0.01)
    assert not entry(rm=rm_with(max_entries_per_day=0)).approved
    assert not entry(rm=rm_with(min_edge_multiple=10.0)).approved      # 2% gain < 10 x 0.4%
    assert entry(rm=rm_with(daily_loss_limit_pct=5.0), pf=replace(GOOD_PF, equity_aud=9_600.0)).approved


# ---------------------------------------------------------------------------
# Exits: reducing risk is never blocked by pause / kill / daily limit
# ---------------------------------------------------------------------------

HELD = Position("BTC", 0.005, 100_000.0, 99_000.0, 102_000.0, T0, 100_000.0)


@pytest.mark.parametrize("reason", PROTECTIVE_EXIT_REASONS)
def test_protective_exits_always_allowed(reason):
    d = RM.check_exit(HELD, 0.005, replace(GOOD_MKT, data_problems=("stale candles",), api_error_streak=5),
                      reason=reason)
    assert d.approved, d.reasons
    assert d.qty == 0.005 and d.price == GOOD_MKT.bid
    assert d.notional_aud == pytest.approx(0.005 * 99_900) and d.fee_aud == pytest.approx(0.005 * 99_900 * 0.001)


@pytest.mark.parametrize("vote,ok", [("SELL", True), ("WAIT", False), ("SKIP", False), ("BUY", False), (None, False)])
def test_discretionary_exit_needs_skeptic_sell(vote, ok):
    d = RM.check_exit(HELD, 0.005, GOOD_MKT, reason="agent_sell", skeptic_vote=vote)
    assert d.approved is ok


@pytest.mark.parametrize("qty,ok", [(0.005, True), (0.002, True), (0.0050001, False), (0.0, False),
                                    (-0.001, False), (NAN, False)])
def test_exit_quantity_rules(qty, ok):
    assert RM.check_exit(HELD, qty, GOOD_MKT, reason="stop_loss").approved is ok


def test_exit_needs_a_fresh_price():
    d = RM.check_exit(HELD, 0.005, replace(GOOD_MKT, quote_age_seconds=500), reason="stop_loss")
    assert not d.approved and "retry" in d.reasons[0]


def test_exit_coin_must_match():
    assert not RM.check_exit(HELD, 0.005, replace(GOOD_MKT, coin="ETH"), reason="stop_loss").approved


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def test_decisions_are_recorded(db, clock):
    d = entry(ctl=Controls(paused=True))
    rid = record_decision(db, clock, d, cycle_id="c9", debate_id=4)
    row = db.query_one("SELECT * FROM risk_decisions WHERE id = ?", (rid,))
    assert row["approved"] == 0 and row["cycle_id"] == "c9" and row["debate_id"] == 4
    assert json.loads(row["failures"])[0].startswith("not_paused")
    assert len(json.loads(row["checks"])) == len(d.checks)


# ---------------------------------------------------------------------------
# Property tests: thousands of random situations; an approved trade must
# ALWAYS satisfy every rule. If any combination sneaks through, this fails.
# ---------------------------------------------------------------------------

floats = st.floats(allow_nan=True, allow_infinity=True)


def _mostly(only_good: bool):
    def mostly(good, anything):
        """Draw from the 'good' range about half the time, otherwise from the full range.
        Keeps every rule biased towards passing, so plenty of trades get approved
        and the invariants below are genuinely exercised."""
        return good if only_good else st.one_of(good, anything)
    return mostly


@st.composite
def situations(draw, only_good: bool = False):
    mostly = _mostly(only_good)
    ask = draw(st.floats(min_value=0.01, max_value=200_000))
    spread = draw(mostly(st.floats(min_value=0, max_value=0.003), st.floats(min_value=0, max_value=0.02)))
    bid = ask * (1 - spread)
    stop_frac = draw(mostly(st.floats(min_value=0.003, max_value=0.05), st.floats(min_value=-0.01, max_value=0.08)))
    rr = draw(mostly(st.floats(min_value=1.5, max_value=6), st.floats(min_value=-1, max_value=6)))
    stop = ask * (1 - stop_frac)
    gain_frac = max(stop_frac, 0.001) * rr
    if only_good:   # make sure the target also clears 3x the round-trip cost
        cost_frac = (2 * CFG.fees.market_order_pct + spread * 100 + 2 * CFG.fees.slippage_estimate_pct) / 100
        gain_frac = max(gain_frac, CFG.risk.min_edge_multiple * cost_frac * 1.01)
    tp = ask * (1 + gain_frac)
    equity = draw(st.floats(min_value=1_000 if only_good else 1, max_value=1_000_000))
    pf = PortfolioState(
        equity_aud=equity,
        cash_aud=draw(mostly(st.just(equity), st.floats(min_value=0, max_value=equity))),
        day_start_equity_aud=equity * draw(mostly(st.floats(min_value=0.95, max_value=1.025),
                                                  st.floats(min_value=0.9, max_value=1.1))),
        open_positions=tuple(pos(c) for c in draw(mostly(
            st.lists(st.sampled_from(["ETH", "SOL", "XRP", "ADA"]), max_size=2, unique=True),
            st.lists(st.sampled_from(["ETH", "SOL", "XRP", "ADA"]), max_size=4, unique=True)))),
        entries_today=draw(mostly(st.integers(min_value=0, max_value=5), st.integers(min_value=0, max_value=8))),
        live_exposure_aud=draw(mostly(st.just(0.0), st.floats(min_value=0, max_value=500))),
    )
    req = EntryRequest(
        "BTC",
        ask * (1 + draw(mostly(st.floats(min_value=-0.004, max_value=0.004), st.floats(min_value=-0.01, max_value=0.01)))),
        stop, tp,
        draw(mostly(st.floats(min_value=60, max_value=100), st.floats(min_value=0, max_value=100))),
        draw(st.sampled_from(["BUY"] if only_good else ["BUY"] * 6 + ["WAIT", "SKIP", "SELL", None])),
    )
    mkt = MarketState("BTC", bid, ask,
                      draw(mostly(st.floats(min_value=0, max_value=120), st.floats(min_value=0, max_value=200))),
                      api_error_streak=draw(mostly(st.just(0), st.integers(min_value=0, max_value=4))))
    ctl = Controls(killed=False if only_good else draw(st.sampled_from([False] * 9 + [True])),
                   paused=False if only_good else draw(st.sampled_from([False] * 9 + [True])),
                   live=draw(st.booleans()),
                   live_capital_cap_aud=draw(st.sampled_from([200.0, 1_000.0] if only_good
                                                             else [None, 200.0, 1_000.0, 1_000.0])))
    return req, pf, mkt, ctl


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.one_of(situations(), situations(only_good=True)))
def test_property_approved_trades_obey_every_rule(sit):
    req, pf, mkt, ctl = sit
    d = RM.check_entry(req, pf, mkt, ctl)
    if not d.approved:
        assert d.qty == 0 and d.notional_aud == 0
        return
    r, tol = CFG.risk, 1e-6
    total = d.notional_aud + d.fee_aud
    assert total <= pf.equity_aud * r.max_position_pct / 100 + tol
    worst = d.qty * d.max_price * (1 + CFG.fees.market_order_pct / 100)     # filled at the worst accepted price
    assert worst <= pf.equity_aud * r.max_position_pct / 100 + tol
    assert worst <= pf.cash_aud + tol
    assert total <= pf.cash_aud + tol
    if ctl.live:
        assert ctl.live_capital_cap_aud is not None
        assert pf.live_exposure_aud + total <= ctl.live_capital_cap_aud + tol
    assert d.notional_aud >= r.min_order_aud
    assert req.stop_loss_aud < mkt.ask < req.take_profit_aud
    assert (1 - req.stop_loss_aud / mkt.ask) * 100 <= r.max_stop_distance_pct + tol
    assert (req.take_profit_aud - mkt.ask) / (mkt.ask - req.stop_loss_aud) >= r.min_reward_risk - tol
    spread = (mkt.ask - mkt.bid) / ((mkt.ask + mkt.bid) / 2) * 100
    assert (req.take_profit_aud / mkt.ask - 1) * 100 >= r.min_edge_multiple * RM.round_trip_cost_pct(spread) - tol
    assert req.confidence >= r.min_confidence and req.skeptic_vote == "BUY"
    assert len(pf.open_positions) < r.max_open_positions and pf.entries_today < r.max_entries_per_day
    assert pf.daily_pnl_pct > -r.daily_loss_limit_pct
    assert not ctl.killed and not ctl.paused
    assert mkt.quote_age_seconds <= CFG.data.max_price_age_seconds
    # sending the amount to CoinSpot with 8 decimals must not change it
    assert float(f"{d.qty:.8f}") == pytest.approx(d.qty, rel=1e-12, abs=1e-12)


@settings(max_examples=1500, deadline=None)
@given(floats, floats, floats, floats, floats, floats, floats, floats, floats)
def test_property_never_crashes_on_any_numbers(e, s, t, c, b, a, eq, cash, start):
    d = RM.check_entry(EntryRequest("BTC", e, s, t, c, "BUY"),
                       PortfolioState(eq, cash, start), MarketState("BTC", b, a, 1.0), Controls())
    if d.approved:
        assert all(math.isfinite(x) and x > 0 for x in (e, s, t, b, a, eq, start))
        assert d.notional_aud + d.fee_aud <= eq * CFG.risk.max_position_pct / 100 + 1e-6


def test_property_tests_actually_produce_approvals():
    """Guard against the property test passing only because nothing is ever approved."""
    from hypothesis import find
    found = find(situations(), lambda s: RM.check_entry(*s).approved,
                 settings=settings(max_examples=5000, deadline=None, database=None))
    assert RM.check_entry(*found).approved


@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(situations(only_good=True))
def test_property_good_trades_are_never_wrongly_rejected(sit):
    """Every rule satisfied (any price from A$0.01 to A$200k, any portfolio size) -> approved."""
    d = RM.check_entry(*sit)
    assert d.approved, d.reasons
