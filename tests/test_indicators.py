import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from desk import indicators as ind

# StockCharts' published RSI(14) worked example (the classic Wilder dataset).
SC_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
             45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
             46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
             43.4205, 42.6628, 43.1314]
SC_RSI = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99,
          41.46, 41.87, 45.46, 37.30, 33.08, 37.77]


def ref_rsi(closes, period=14):
    """Independent textbook implementation of Wilder's RSI."""
    out = [math.nan] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period

    def val(g, l):
        if l == 0:
            return 50.0 if g == 0 else 100.0
        return 100 - 100 / (1 + g / l)

    out[period] = val(ag, al)
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = val(ag, al)
    return out


def test_rsi_matches_stockcharts_example():
    r = ind.rsi(pd.Series(SC_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    assert list(r.iloc[14:].round(2)) == pytest.approx(SC_RSI, abs=0.011)


def test_rsi_matches_reference_on_random_walk():
    rng = np.random.default_rng(7)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 300)))
    ours = ind.rsi(pd.Series(closes), 14).to_numpy()
    ref = np.array(ref_rsi(closes, 14))
    assert np.allclose(ours[14:], ref[14:], atol=1e-9)


def test_rsi_extremes():
    assert ind.rsi(pd.Series(np.arange(1, 50, dtype=float)), 14).iloc[-1] == 100.0
    assert ind.rsi(pd.Series(np.arange(50, 1, -1, dtype=float)), 14).iloc[-1] == pytest.approx(0.0)
    assert ind.rsi(pd.Series([5.0] * 40), 14).iloc[-1] == 50.0


@settings(max_examples=60, deadline=None)
@given(st.lists(st.floats(min_value=0.01, max_value=1e6, allow_nan=False), min_size=20, max_size=120))
def test_rsi_always_between_0_and_100(closes):
    r = ind.rsi(pd.Series(closes), 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_ema_seeded_with_sma_then_recursive():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    e = ind.ema(s, 3)
    assert e.iloc[:2].isna().all()
    assert e.iloc[2] == pytest.approx(2.0)                    # SMA(1,2,3)
    assert e.iloc[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)    # alpha = 2/(3+1)
    assert e.iloc[5] == pytest.approx(0.5 * 6 + 0.5 * (0.5 * 5 + 0.5 * 3.0))


def test_ema_skips_leading_nans():
    s = pd.Series([np.nan, np.nan, 1.0, 2.0, 3.0, 4.0])
    e = ind.ema(s, 3)
    assert e.iloc[:4].isna().all()
    assert e.iloc[4] == pytest.approx(2.0)


def test_ema_constant_series():
    assert ind.ema(pd.Series([7.0] * 30), 10).iloc[-1] == pytest.approx(7.0)


def test_macd_parts_consistent():
    rng = np.random.default_rng(1)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    m = ind.macd(close, 12, 26, 9)
    line = ind.ema(close, 12) - ind.ema(close, 26)
    assert np.allclose(m["macd"].dropna(), line.dropna())
    assert np.allclose((m["macd"] - m["signal"]).dropna(), m["hist"].dropna())
    assert m["hist"].iloc[:33].isna().all() and not math.isnan(m["hist"].iloc[33])   # 26 + 9 - 2


def test_macd_positive_in_steady_uptrend():
    close = pd.Series(100 * 1.01 ** np.arange(100))
    assert ind.macd(close).iloc[-1]["macd"] > 0


def test_true_range_and_atr_hand_computed():
    df = pd.DataFrame({
        "open":  [10, 11, 12, 11],
        "high":  [11, 13, 12.5, 12],
        "low":   [9, 10.5, 10, 9],
        "close": [10.5, 12, 11, 9.5],
        "volume": [1, 1, 1, 1],
    }, dtype=float)
    tr = ind.true_range(df)
    # bar0: 11-9=2 ; bar1: max(2.5, |13-10.5|=2.5, |10.5-10.5|=0)=2.5
    # bar2: max(2.5, |12.5-12|=.5, |10-12|=2)=2.5 ; bar3: max(3, |12-11|=1, |9-11|=2)=3
    assert list(tr) == [2, 2.5, 2.5, 3]
    a = ind.atr(df, 3)
    assert a.iloc[2] == pytest.approx(7 / 3)
    assert a.iloc[3] == pytest.approx((7 / 3 * 2 + 3) / 3)


def test_volume_ratio_and_highest():
    vol = pd.Series([10.0] * 20 + [30.0])
    assert ind.volume_ratio(vol, 20).iloc[-1] == pytest.approx(3.0)
    assert ind.volume_ratio(vol, 20).iloc[:20].isna().all()
    hi = ind.highest(pd.Series([1.0, 5, 3, 4, 2]), 3)
    assert hi.iloc[3] == 5 and hi.iloc[4] == 5
    assert ind.highest(pd.Series([1.0, 5, 3, 4, 2, 9]), 3).iloc[5] == 4     # current bar excluded
