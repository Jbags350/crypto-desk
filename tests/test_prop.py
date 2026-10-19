"""Prop-account simulator: fills, stops, costs, swap, firm limits, brake, phases, payouts."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from desk.prop.challenge import attempt
from desk.prop.market import Market, _daily_on_hours, build_market
from desk.prop.rules import PropRules, tradeify_2step
from desk.prop.sim import SimConfig, simulate
from desk.prop.strategies import Signals, _blank, trend


def make_market(closes: dict[str, list[float]], start="2026-01-01 00:00", lows=None, highs=None, opens=None):
    idx = pd.date_range(start, periods=len(next(iter(closes.values()))), freq="1h", tz="UTC")
    coins = list(closes)
    c = np.column_stack([np.asarray(closes[k], float) for k in coins])
    o = np.column_stack([np.asarray((opens or {}).get(k, closes[k]), float) for k in coins])
    lo = np.column_stack([np.asarray((lows or {}).get(k, closes[k]), float) for k in coins])
    hi = np.column_stack([np.asarray((highs or {}).get(k, closes[k]), float) for k in coins])
    lo = np.minimum(lo, np.minimum(o, c))
    hi = np.maximum(hi, np.maximum(o, c))
    return Market(idx, coins, o, hi, lo, c, np.ones_like(c), np.ones_like(c, bool), {})


def no_cost_rules(**kw) -> PropRules:
    base = PropRules("test", 100_000, 500, (10.0, 5.0), 3.0, 6.0, fee_pct=0.0, swap_pct_per_day=0.0,
                     spread_pct={}, default_spread_pct=0.0, slippage_pct=0.0, stop_slippage_pct=0.0,
                     leverage={}, default_leverage=100.0)
    return replace(base, **kw)


def signals_at(m: Market, entries: dict[tuple[int, int], int], stop: float, target=np.nan, trail=np.nan,
               max_hold=10_000) -> Signals:
    s = _blank(m, "test", max_hold)
    for (t, j), side in entries.items():
        s.entry[t, j] = side
        s.score[t, j] = 1.0
    s.stop_dist[:] = stop
    s.target_dist[:] = target
    s.trail_dist[:] = trail
    return s


CFG = SimConfig(risk_pct=1.0, daily_brake_pct=None, enforce_rules=False)


def test_long_stop_loses_exactly_the_risk():
    m = make_market({"BTC": [100, 100, 100, 99, 98]}, lows={"BTC": [100, 100, 100, 95, 98]})
    sig = signals_at(m, {(1, 0): 1}, stop=2.0)                 # stop at 98
    res = simulate(m, sig, no_cost_rules(), CFG, 0)
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr["reason"] == "stop" and tr["exit"] == pytest.approx(98.0)
    assert tr["pnl"] == pytest.approx(-1000.0)                  # 1% of 100k
    assert res.equity[-1] == pytest.approx(99_000)


def test_gap_through_the_stop_fills_at_the_open():
    m = make_market({"BTC": [100, 100, 100, 90, 90]}, opens={"BTC": [100, 100, 100, 90, 90]})
    sig = signals_at(m, {(1, 0): 1}, stop=2.0)
    res = simulate(m, sig, no_cost_rules(), CFG, 0)
    assert res.trades[0]["exit"] == pytest.approx(90.0)
    assert res.trades[0]["pnl"] == pytest.approx(-5000.0)       # the gap costs 5x the planned risk


def test_short_take_profit():
    m = make_market({"ETH": [100, 100, 99, 97, 96]}, lows={"ETH": [100, 100, 99, 95, 96]})
    sig = signals_at(m, {(1, 0): -1}, stop=2.0, target=4.0)    # short at 100, tp 96
    res = simulate(m, sig, no_cost_rules(), CFG, 0)
    tr = res.trades[0]
    assert tr["side"] == -1 and tr["reason"] == "target" and tr["exit"] == pytest.approx(96.0)
    assert tr["pnl"] == pytest.approx(2000.0)                   # 2R


def test_stop_wins_when_a_candle_touches_both():
    m = make_market({"BTC": [100, 100, 100, 100]}, lows={"BTC": [100, 100, 97, 100]},
                    highs={"BTC": [100, 100, 105, 100]})
    sig = signals_at(m, {(1, 0): 1}, stop=2.0, target=4.0)
    res = simulate(m, sig, no_cost_rules(), CFG, 0)
    assert res.trades[0]["reason"] == "stop"


def test_fees_spread_and_slippage_are_charged():
    rules = no_cost_rules(fee_pct=0.04, default_spread_pct=0.10, slippage_pct=0.02)
    m = make_market({"BTC": [100.0] * 6})
    sig = signals_at(m, {(1, 0): 1}, stop=2.0, max_hold=2)
    res = simulate(m, sig, rules, CFG, 0)
    tr = res.trades[0]
    half = 0.05 + 0.02                                           # % per fill
    assert tr["entry"] == pytest.approx(100 * (1 + half / 100))
    assert tr["exit"] == pytest.approx(100 * (1 - half / 100))
    qty = 1000 / 2.0
    expected = -qty * (tr["entry"] - tr["exit"]) - qty * tr["entry"] * 0.0004 - qty * tr["exit"] * 0.0004
    assert tr["pnl"] == pytest.approx(expected)


def test_swap_is_charged_at_the_22_utc_reset():
    rules = no_cost_rules(swap_pct_per_day=0.033)
    m = make_market({"BTC": [100.0] * 30}, start="2026-01-01 18:00")   # candle 3 closes at 22:00
    sig = signals_at(m, {(0, 0): 1}, stop=10.0)
    res = simulate(m, sig, rules, CFG, 0)
    qty = 1000 / 10.0
    # 30 candles from 18:00 cross 22:00 twice (candles 3 and 27)
    assert res.trades[0]["swap"] == pytest.approx(2 * qty * 100 * 0.00033)


def test_leverage_caps_the_position():
    rules = no_cost_rules(default_leverage=2.0)
    m = make_market({"XRP": [1.0] * 5})
    sig = signals_at(m, {(1, 0): 1}, stop=0.0001)               # tiny stop -> huge size wanted
    res = simulate(m, sig, rules, replace(CFG, margin_use=1.0), 0)
    assert res.trades[0]["qty"] * 1.0 == pytest.approx(200_000)  # 2:1 on 100k


def test_daily_limit_breach_ends_the_run():
    m = make_market({"BTC": [100, 100, 100, 100]}, lows={"BTC": [100, 100, 96.5, 100]})
    sig = signals_at(m, {(1, 0): 1}, stop=10.0)                 # risk 1% at stop 90; size 10k units
    res = simulate(m, sig, no_cost_rules(), replace(CFG, risk_pct=10.0, enforce_rules=True), 0)
    # 100k / 10 * 10% = 1,000 units; dip to 96.5 = -3,500 > 3% daily limit
    assert res.outcome == "breach_daily"


def test_brake_closes_everything_before_the_firm_limit():
    m = make_market({"BTC": [100, 100, 100, 100, 100]}, lows={"BTC": [100, 100, 96.5, 100, 100]})
    sig = signals_at(m, {(1, 0): 1, (3, 0): 1}, stop=10.0)
    cfg = replace(CFG, risk_pct=10.0, enforce_rules=True, daily_brake_pct=2.0, brake_slip_pct=0.25)
    res = simulate(m, sig, no_cost_rules(), cfg, 0)
    assert res.outcome == "end" and res.brakes == 1
    assert res.trades[0]["reason"] == "brake"
    assert res.trades[0]["pnl"] == pytest.approx(-2250.0)       # 2% brake + 0.25% slippage
    assert len(res.trades) == 1                                  # resting for the day: the 2nd signal is ignored


def test_max_loss_floor_is_static():
    closes = [100.0] * 60
    lows = [100.0] * 60
    lows[30] = 94.0                                              # next day: a -6.0% move in one position
    m = make_market({"BTC": closes}, lows={"BTC": lows}, start="2026-01-01 00:00")
    sig = signals_at(m, {(1, 0): 1}, stop=50.0)
    rules = no_cost_rules(daily_loss_pct=50.0)                   # only test the overall floor
    cfg = replace(CFG, risk_pct=50.0, enforce_rules=True, margin_use=1.0)
    res = simulate(m, sig, rules, cfg, 0)
    # 1,000 units: -6 * 1000 = -6,000 -> equity 94,000 = exactly the floor (not below): survives
    assert res.outcome == "end"
    lows[30] = 93.9
    m = make_market({"BTC": closes}, lows={"BTC": lows})
    assert simulate(m, sig, rules, cfg, 0).outcome == "breach_max"


def test_target_reached_closes_and_passes():
    closes = [100, 100, 105, 111, 120]
    m = make_market({"BTC": closes})
    sig = signals_at(m, {(1, 0): 1}, stop=10.0)                  # 1,000 units at 10% risk
    res = simulate(m, sig, no_cost_rules(), replace(CFG, risk_pct=10.0, target_pct=10.0, enforce_rules=True), 0)
    assert res.outcome == "pass" and res.trades[0]["reason"] == "target_reached"
    assert res.end_t == 3                                        # +11,000 at the close of candle 3


def test_max_positions_and_same_side_cap():
    m = make_market({k: [100.0] * 5 for k in ("A", "B", "C", "D")})
    sig = signals_at(m, {(1, 0): 1, (1, 1): 1, (1, 2): 1, (1, 3): -1}, stop=5.0, max_hold=2)
    res = simulate(m, sig, no_cost_rules(), replace(CFG, max_positions=3, max_same_side=2), 0)
    sides = sorted(t["side"] for t in res.trades)
    assert sides == [-1, 1, 1]


def test_open_profit_cant_be_withdrawn():
    n = 24 * 40
    closes = list(np.linspace(100, 110, n))
    m = make_market({"BTC": closes}, start="2026-01-01 22:00")
    sig = signals_at(m, {(0, 0): 1}, stop=10.0)
    res = simulate(m, sig, no_cost_rules(), replace(CFG, risk_pct=10.0, payout_every_days=14,
                                                    enforce_rules=True), 0)
    assert res.payouts == 0.0                     # the trade was still open at every payout date


def test_payout_is_80_percent_of_closed_profit():
    n = 24 * 16
    closes = [100.0] * n
    closes[5] = 110.0
    m = make_market({"BTC": closes}, start="2026-01-01 22:00")
    sig = signals_at(m, {(0, 0): 1}, stop=10.0, target=9.0)     # tp 109; candle 5 opens at 110 -> fills at 110
    res = simulate(m, sig, no_cost_rules(), replace(CFG, risk_pct=10.0, payout_every_days=14,
                                                    enforce_rules=True), 0)
    assert res.trades[0]["exit"] == pytest.approx(110.0)
    assert res.payouts == pytest.approx(10_000 * 0.8)


def test_challenge_runs_both_phases_then_funded():
    closes = list(np.linspace(100, 140, 24 * 60))
    m = make_market({"BTC": closes}, start="2026-01-01 00:00")
    sig = signals_at(m, {(t, 0): 1 for t in range(0, 24 * 60, 6)}, stop=5.0, max_hold=5)
    rules = no_cost_rules()
    a = attempt(m, sig, rules, SimConfig(risk_pct=5.0, daily_brake_pct=2.0), 0, funded_days=10)
    assert a.passed and [p[0] for p in a.phases] == ["pass", "pass"]
    assert a.funded_days > 0


# ---- no peeking ahead ---------------------------------------------------------------

def test_daily_values_only_appear_after_the_day_closes():
    days = pd.date_range("2026-01-01", periods=3, freq="1D", tz="UTC")
    daily = pd.Series([1.0, 2.0, 3.0], index=days)
    hours = pd.date_range("2026-01-01", periods=72, freq="1h", tz="UTC")
    mapped = _daily_on_hours(daily, hours)
    # candle 0..22 of Jan 1 close before the day closes: nothing known yet
    assert np.isnan(mapped[:23]).all()
    # the 23:00 candle closes at 00:00 Jan 2 = Jan 1's close: value 1 is now known
    assert mapped[23] == 1.0 and mapped[46] == 1.0 and mapped[47] == 2.0


def test_trend_signals_only_at_the_daily_close():
    rng = np.random.default_rng(1)
    n = 24 * 120
    price = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n)))
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": price, "high": price * 1.002, "low": price * 0.998, "close": price,
                       "volume": 1.0}, index=idx)
    m = build_market({"BTC": df}, idx[0].to_pydatetime(), (idx[-1] + pd.Timedelta(hours=1)).to_pydatetime())
    s = trend(m)
    rows = np.flatnonzero(s.entry[:, 0] != 0)
    assert len(rows) > 0
    assert set(m.close_hour()[rows]) == {0}


# ---- rotation strategy -------------------------------------------------------------

def rotation_market(rets: list[float], btc_above: bool = True, hours: int = 48):
    """5 coins (BTC first) with fixed recent returns; the 00:00 UTC candle closes are rebalance points."""
    coins = ["BTC", "A", "B", "C", "D"]
    m = make_market({k: [100.0] * hours for k in coins}, start="2026-03-01 00:00")
    shape = (hours, len(coins))
    m.feats["ret336"] = np.tile(np.asarray(rets, float), (hours, 1))
    m.feats["atr20_d"] = np.full(shape, 4.0)
    ema = np.full(shape, 90.0)                                   # every coin above its 50-day average
    ema[:, 0] = 90.0 if btc_above else 110.0
    m.feats["ema50_d"] = ema
    return m


def test_rotation_buys_the_strongest_when_bitcoin_is_up():
    from desk.prop.strategies import rotation
    m = rotation_market([0.01, 0.30, 0.20, -0.10, 0.05])
    s = rotation(m, lookback=336, top=3, shorts=False)
    rows = np.flatnonzero(m.close_hour() == 0)
    assert len(rows) == 2                                        # 23:00 candles close at 00:00
    t = rows[0]
    assert list(s.entry[t]) == [0, 1, 1, 0, 1]                   # A, B and D (BTC is 4th strongest)
    assert list(s.exit_long[t]) == [True, False, False, True, False]
    assert (s.entry[m.close_hour() != 0] == 0).all()             # only at the daily rebalance


def test_rotation_sits_out_when_bitcoin_is_below_its_average():
    from desk.prop.strategies import rotation
    m = rotation_market([0.01, 0.30, 0.20, -0.10, 0.05], btc_above=False)
    s = rotation(m, lookback=336, top=3, shorts=False)
    assert (s.entry == 0).all()
    t = np.flatnonzero(m.close_hour() == 0)[0]
    assert s.exit_long[t].all()                                  # and closes anything it still holds


def test_rotation_never_buys_a_falling_coin():
    from desk.prop.strategies import rotation
    m = rotation_market([-0.05, -0.30, -0.20, -0.10, -0.01])
    s = rotation(m, lookback=336, top=3, shorts=False)
    assert (s.entry == 0).all()


# ---- Instant Funding rules ---------------------------------------------------------

def instant_rules(**kw):
    return no_cost_rules(account_size=10_000, trailing_floor=True, daily_ref="balance", consistency_pct=20.0,
                         payout_locks_floor=True, daily_loss_pct=50.0, **kw)


def test_instant_floor_trails_closed_balance_up_to_the_start():
    # +$300 closed: floor 9,400 -> 9,700. Then a dip that would have been fine with a static floor breaches.
    closes = [100.0, 100.0, 103.0, 103.0, 103.0, 103.0]
    lows = [100.0, 100.0, 103.0, 103.0, 99.0, 103.0]
    m = make_market({"BTC": closes}, lows={"BTC": lows})
    sig = signals_at(m, {(1, 0): 1, (3, 0): 1}, stop=50.0, target=3.0)      # 100 units; tp at 103
    res = simulate(m, sig, instant_rules(), replace(CFG, risk_pct=50.0, enforce_rules=True, margin_use=1.0), 0)
    # 2nd trade: 100 units from 103 down to 99 = -400 -> equity 9,900 < floor 9,700? no: 10,300-400 = 9,900 > 9,700
    assert res.outcome == "end"
    lows[4] = 96.0                                                           # -700 -> 9,600 < 9,700
    m = make_market({"BTC": closes}, lows={"BTC": lows})
    res = simulate(m, sig, instant_rules(), replace(CFG, risk_pct=50.0, enforce_rules=True, margin_use=1.0), 0)
    assert res.outcome == "breach_max"


def test_instant_payout_locks_the_floor_at_the_start():
    n = 24 * 20
    closes = [100.0] * n
    closes[5] = 106.0                                                        # one big winning day
    for d in range(6):                                                       # then 6 small winning days
        closes[24 * (d + 1) + 5] = 100.0
    m = make_market({"BTC": closes}, start="2026-01-01 22:00")
    # a one-day win can't be paid out (100% of the profit came on one day)
    sig = signals_at(m, {(0, 0): 1}, stop=10.0, target=5.0)
    res = simulate(m, sig, instant_rules(), replace(CFG, risk_pct=10.0, payout_every_days=1, enforce_rules=True), 0)
    assert res.payouts == 0 and res.blocked_by_consistency > 0


def test_instant_payout_needs_profit_spread_over_days():
    days = 12
    n = 24 * days
    closes = list(100.0 + np.arange(n) * 0.0)
    m = make_market({"BTC": closes}, start="2026-01-01 22:00")
    sig = signals_at(m, {}, stop=10.0)
    rules = instant_rules()
    # fake a steady realised profit: +$100 a day, by giving the account a winning trade each day
    entries = {}
    for d in range(days - 1):
        t0 = 24 * d + 1
        entries[(t0, 0)] = 1
        closes[t0 + 2] = 101.0                                               # +1% on 100 units = +$100
    m = make_market({"BTC": closes}, start="2026-01-01 22:00")
    sig = signals_at(m, entries, stop=10.0, target=1.0)
    cfg = replace(CFG, risk_pct=10.0, payout_every_days=7, payout_keep_pct=3.0, enforce_rules=True)
    res = simulate(m, sig, rules, cfg, 0)
    assert res.payout_count >= 1
    assert res.equity[-1] >= 10_000 * 1.03 - 1                             # the 3% cushion stays in the account
