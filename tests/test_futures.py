"""Futures day-trading research: strategies on made-up days, and the Tradeify account rules."""

import numpy as np
import pandas as pd
import pytest

from desk.futures.account import DayResult, run_account
from desk.futures.data import Day
from desk.futures.specs import CONTRACTS, select_flex
from desk.futures.strategies import Trade, gap_fade, momentum, orb, trend_day

NQ = CONTRACTS["NQ"]


def make_day(closes, highs=None, lows=None, opens=None, start="09:30", prev_close=100.0, atr=10.0, trend=None):
    n = len(closes)
    h0, m0 = (int(x) for x in start.split(":"))
    minutes = np.arange(h0 * 60 + m0, h0 * 60 + m0 + n)
    c = np.asarray(closes, float)
    o = np.asarray(opens if opens is not None else np.r_[c[0], c[:-1]], float)
    h = np.asarray(highs if highs is not None else np.maximum(o, c), float)
    l = np.asarray(lows if lows is not None else np.minimum(o, c), float)
    return Day(pd.Timestamp("2025-03-04", tz="America/New_York"), minutes, o, h, l, c, prev_close, atr, trend)


def test_trade_dollars_include_costs():
    t = Trade(None, 1, 100.0, 110.0, 2.0, 1, "time")
    # 10 points x $2 = $20, minus $1.50 commission, minus 1 tick (0.25 x $2 = $0.50)
    assert t.dollars(NQ) == pytest.approx(18.0)
    assert t.dollars(NQ, 3) == pytest.approx(54.0)
    assert t.worst_dollars(NQ) == pytest.approx(-2.0 * 2 - 1.5)


def test_orb_breakout_up_runs_to_the_close():
    closes = [100, 101, 102, 101, 100] + [101] * 10 + [103, 105] + [106] * 373
    d = make_day(closes)
    t = orb(d, NQ, minutes=5)
    assert t is not None and t.side == 1
    assert t.entry == pytest.approx(102 + 0.25)                 # range high + 1 tick
    assert t.reason == "time" and t.exit == pytest.approx(106)


def test_orb_stop_at_the_other_side():
    closes = [100, 101, 102, 101, 100] + [101] * 10 + [103] + [98] + [97] * 374
    d = make_day(closes)
    t = orb(d, NQ, minutes=5)
    assert t.reason == "stop" and t.exit == pytest.approx(100 - 0.25)   # range low (100) minus a tick


def test_orb_trend_filter_blocks_trades_against_the_trend():
    closes = [100, 101, 102, 101, 100] + [101] * 10 + [103, 105] + [106] * 373
    assert orb(make_day(closes, trend=False), NQ, minutes=5, trend_filter=True) is None
    assert orb(make_day(closes, trend=True), NQ, minutes=5, trend_filter=True) is not None


def test_momentum_follows_the_first_half_hour():
    closes = [100.0] * 390
    closes[:30] = list(np.linspace(100, 104, 30))                # up from the prior close of 100
    closes[360:] = list(np.linspace(104, 106, 30))
    d = make_day(closes, prev_close=100.0)
    t = momentum(d, NQ)
    assert t.side == 1 and t.exit > t.entry


def test_gap_fade_targets_the_prior_close():
    closes = [105.0] + [104, 103, 101, 99.5] + [100] * 385
    d = make_day(closes, opens=[105.0] + closes[:-1], prev_close=100.0, atr=10.0)
    t = gap_fade(d, NQ, min_gap_atr=0.3)
    assert t.side == -1 and t.reason == "target" and t.exit == pytest.approx(100.0)


def test_trend_day_needs_a_big_move_by_mid_morning():
    flat = make_day([100.0] * 390)
    assert trend_day(flat, NQ) is None
    run = make_day(list(np.linspace(100, 110, 60)) + list(np.linspace(110, 115, 330)), atr=10.0)
    t = trend_day(run, NQ, at="10:30", min_move_atr=0.5)
    assert t.side == 1 and t.reason == "time"


# ---- the account ------------------------------------------------------------------------

def test_eval_passes_at_target_with_consistency():
    rules = select_flex(100_000)
    days = [DayResult(1_000, -200)] * 10                         # $1,000 a day: best day 1/6 of $6,000
    r = run_account(days, rules, 0)
    assert r.outcome == "pass" and r.days == 6


def test_one_huge_day_doesnt_pass_until_profit_is_spread_out():
    rules = select_flex(100_000)
    days = [DayResult(6_000, 0)] + [DayResult(500, 0)] * 20
    r = run_account(days, rules, 0)
    # best day 6,000 must be <= 40% of profit -> needs profit >= 15,000: 6,000 + 18 x 500
    assert r.outcome == "pass" and r.days == 19


def test_trailing_loss_limit_follows_end_of_day_highs_and_locks():
    rules = select_flex(100_000)
    days = [DayResult(2_000, 0), DayResult(0, -4_900)]           # high 102,000 -> limit 99,000; worst 97,100
    assert run_account(days, rules, 0, mode="funded").outcome == "breach"
    days = [DayResult(2_000, 0), DayResult(0, -2_900)]           # worst 99,100 > 99,000: survives
    assert run_account(days, rules, 0, mode="funded").outcome == "end"
    days = [DayResult(5_000, 0), DayResult(-1_000, 0), DayResult(0, -3_850)]   # locked at 100,100
    assert run_account(days, rules, 0, mode="funded").outcome == "end"
    days = [DayResult(5_000, 0), DayResult(-1_000, 0), DayResult(0, -3_950)]
    assert run_account(days, rules, 0, mode="funded").outcome == "breach"


def test_funded_payouts_every_five_winning_days():
    rules = select_flex(100_000)
    days = [DayResult(600, 0)] * 12
    r = run_account(days, rules, 0, mode="funded")
    assert len(r.payouts) == 2                                     # after day 5 and day 10
    assert r.payouts[0][1] == pytest.approx(min(3_500, 0.5 * 3_000) * 0.9)


def test_static_account_two_phases_and_limits():
    from desk.futures.account import run_static
    win = [DayResult(2_000, -500)] * 20
    r = run_static(win, 100_000, (10.0, 5.0), 5.0, 10.0, 0)
    assert r.outcome == "pass" and r.days == 5 + 3                      # +10% in 5 days, then +5% in 3
    bad = [DayResult(-1_000, -5_100)]
    assert run_static(bad, 100_000, (10.0,), 5.0, 10.0, 0).outcome == "breach"     # daily limit
    slow = [DayResult(-2_000, -2_000)] * 6
    assert run_static(slow, 100_000, (10.0,), 5.0, 10.0, 0).outcome == "breach"   # 10% overall on day 6


def test_static_account_funded_payouts():
    from desk.futures.account import run_static
    r = run_static([DayResult(300, 0)] * 25, 100_000, (), 5.0, 10.0, 0, payout_every=10)
    assert len(r.payouts) == 2 and r.payouts[0][1] == pytest.approx(11 * 300 * 0.8)


# ---- Goldman Sachs Xenith intraday momentum ------------------------------------------------

def test_twap_momentum_goes_short_on_a_selloff_and_exits_at_the_close():
    from desk.futures.strategies import twap_momentum
    closes = list(np.linspace(99, 95, 390))                  # falls all day from yesterday's 100
    d = make_day(closes, prev_close=100.0)
    r = twap_momentum(d, CONTRACTS["ES"], side="short", start_pct=0.5, full_pct=1.5, max_step=1.0)
    assert r.pnl_pts > 0                                       # short into a falling market makes money
    # builds up to (almost) a full short, then all of it is bought back at the close; within the 10% band
    # it stops adjusting, so it may finish slightly under 100% short
    assert 1.8 <= r.turnover <= 2.0


def test_twap_momentum_short_only_ignores_rallies():
    from desk.futures.strategies import twap_momentum
    d = make_day(list(np.linspace(101, 105, 390)), prev_close=100.0)
    r = twap_momentum(d, CONTRACTS["ES"], side="short")
    assert r.pnl_pts == 0 and r.turnover == 0
    r = twap_momentum(d, CONTRACTS["ES"], side="long", start_pct=0.5, full_pct=1.5)
    assert r.pnl_pts > 0


def test_twap_momentum_loses_on_a_reversal_and_reports_the_worst_moment():
    from desk.futures.strategies import twap_momentum
    closes = list(np.linspace(99, 97, 120)) + list(np.linspace(97, 101, 270))   # sell-off, then rally
    d = make_day(closes, prev_close=100.0)
    r = twap_momentum(d, CONTRACTS["ES"], side="short", start_pct=0.5, full_pct=1.5, max_step=1.0)
    assert r.pnl_pts < 0 and r.worst_pts <= r.pnl_pts
