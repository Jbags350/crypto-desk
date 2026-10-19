"""Walk-forward learning: it may only ever use the past."""

import numpy as np
import pytest

from desk.prop.learn import Candidate, Logistic, learn_mistakes, learn_settings, stitch, window_score
from desk.prop.strategies import _blank
from tests.test_prop import make_market

DAY = 24


def test_logistic_learns_a_simple_pattern():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(600, 2))
    y = (x[:, 0] + 0.3 * rng.normal(size=600) > 0).astype(float)      # only the first input matters
    model = Logistic().fit(x, y)
    assert model.w[1] > 1.0 and abs(model.w[2]) < 0.5
    assert model.predict(np.array([[2.0, 0.0]]))[0] > 0.9


def test_window_score_rewards_return_per_unit_of_drop():
    eq = np.array([100.0, 110.0, 105.0, 120.0])
    assert window_score(eq, 100.0, 0, 4) == pytest.approx(0.20 / max(0.05, 0.03))


def make_months_market(days=200):
    return make_market({"BTC": [100.0] * (days * DAY)}, start="2026-01-01 00:00")


def test_settings_learner_only_looks_back():
    m = make_months_market()
    n = m.T
    good_early = np.concatenate([np.linspace(100, 200, n // 2), np.linspace(200, 100, n - n // 2)])
    good_late = np.concatenate([np.full(n // 2, 100.0), np.linspace(100, 300, n - n // 2)])
    cands = [Candidate("A", "trend", ()), Candidate("B", "trend", ())]
    eqs = {"A": good_early, "B": good_late}
    months = learn_settings(m, cands, eqs, 100.0, 60 * DAY, n, train_days=50, step_days=30)
    assert months[0].choice == "A"                        # at day 60 only A had done well
    # changing the future must not change a past decision
    eqs2 = {"A": good_early.copy(), "B": good_late.copy()}
    eqs2["B"][90 * DAY:] = 1e6
    assert learn_settings(m, cands, eqs2, 100.0, 60 * DAY, n, train_days=50, step_days=30)[0].choice == "A"


def test_settings_learner_sits_out_when_nothing_worked():
    m = make_months_market()
    falling = np.linspace(100, 50, m.T)
    months = learn_settings(m, [Candidate("A", "trend", ())], {"A": falling}, 100.0, 60 * DAY, m.T)
    assert all(mo.choice is None for mo in months)


def test_stitch_follows_each_months_choice():
    from desk.prop.learn import Month
    m = make_months_market(days=60)
    a, b = _blank(m, "a", 10), _blank(m, "b", 10)
    a.entry[:] = 1
    b.entry[:] = -1
    out = stitch(m, {"A": a, "B": b}, [Month(0, 30 * DAY, "A", 1.0), Month(30 * DAY, 60 * DAY, "B", 1.0)])
    assert (out.entry[: 30 * DAY] == 1).all() and (out.entry[30 * DAY:] == -1).all()


def test_mistake_filter_waits_for_enough_lessons_and_only_uses_finished_trades():
    m = make_months_market(days=200)
    m.feats.update({k: np.zeros((m.T, 1)) for k in ("ret24", "ret168", "ret336", "rsi14_h")})
    m.feats["atr20_d"] = np.ones((m.T, 1))
    m.feats["ema50_d"] = np.full((m.T, 1), 100.0)
    m.feats["vol_ratio"] = np.ones((m.T, 1))
    rng = np.random.default_rng(1)
    hot = rng.random(m.T) < 0.5                            # a feature that decides wins and losses
    m.feats["ret24"][:, 0] = np.where(hot, 0.05, -0.05)
    sig = _blank(m, "s", 10)
    rows = np.arange(0, m.T - 30, 12)
    sig.entry[rows, 0] = 1
    trades = [{"open_t": int(t), "close_t": int(t) + 10, "coin": "BTC", "side": 1, "pnl": 10.0 if hot[t] else -10.0}
              for t in rows]
    out, log = learn_mistakes(m, sig, trades, 10 * DAY, m.T, min_trades=60, skip_share=0.3)
    assert log[0].skipped == 0 and log[0].trained_on < 60          # not enough lessons yet in month 1
    later = slice(100 * DAY, m.T)
    kept = np.flatnonzero(out.entry[later, 0])
    assert len(kept) and hot[100 * DAY + kept].mean() > 0.9         # it learned to keep the winners' setup
