"""Walk-forward learning: the bot re-learns at the start of every month, from the past only.

Two kinds of learning, both judged only on months they haven't seen:

1. SETTINGS. Every candidate way of trading (strategy + parameters, see default_grid) is
   simulated once over the whole history. At the start of each month, each candidate is
   scored on the previous `train_days` only, and the best one trades the coming month.
   If nothing made money recently, the bot sits the month out.

2. MISTAKES. Every finished trade is a lesson: what the market looked like when it was
   opened (features below) and whether it lost. At the start of each month a small
   statistical model (logistic regression) is fitted to all trades that had already
   finished, and new trades that look like past losers (the bottom `skip_share` by
   predicted chance of winning) are skipped.

Because every decision uses only data from before it, the stitched-together months are an
honest test: this is how the live bot would have done had it learned this way.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from desk.prop import strategies as S
from desk.prop.market import Market
from desk.prop.rules import PropRules
from desk.prop.sim import SimConfig, simulate
from desk.prop.strategies import Signals

DAY = 24


@dataclass(frozen=True)
class Candidate:
    name: str
    fn: str
    params: tuple                      # (("key", value), ...) so it can be hashed

    def build(self, m: Market) -> Signals:
        return getattr(S, self.fn)(m, **dict(self.params))


def default_grid() -> list[Candidate]:
    grid = []
    for bd in (10, 20, 40):
        for e in (20, 50, 100):
            for k in (2.0, 3.0):
                grid.append(Candidate(f"trend {bd}d/ema{e}/stop{k:g} long", "trend",
                                      (("breakout_days", bd), ("ema_days", e), ("stop_atr", k), ("shorts", False))))
    for bd in (20, 40):
        for e in (50, 100):
            grid.append(Candidate(f"trend {bd}d/ema{e}/stop3 long+short", "trend",
                                  (("breakout_days", bd), ("ema_days", e), ("stop_atr", 3.0), ("shorts", True))))
    for lb in (168, 336, 720):
        for top in (1, 2, 4):
            grid.append(Candidate(f"rotation {lb // 24}d top{top}", "rotation",
                                  (("lookback", lb), ("top", top), ("stop_atr_d", 2.5), ("shorts", False))))
    return grid


# ---- 1. learning the settings --------------------------------------------------------

@dataclass
class Month:
    start_t: int
    end_t: int
    choice: str | None                 # None = sit out
    score: float


def window_score(equity: np.ndarray, acct: float, a: int, b: int) -> float:
    """Return over the window divided by its worst drop (at least 3% so tiny drops don't dominate)."""
    seg = equity[a:b]
    if len(seg) < 2:
        return float("-inf")
    start = equity[a - 1] if a > 0 else acct
    ret = (seg[-1] - start) / acct
    peak = np.maximum.accumulate(np.concatenate([[start], seg]))[1:]
    dd = float(((seg - peak) / acct).min())
    return ret / max(abs(dd), 0.03)


def learn_settings(m: Market, cands: list[Candidate], equities: dict[str, np.ndarray], acct: float,
                   start_t: int, end_t: int, train_days: int = 730, step_days: int = 30,
                   stickiness: float = 0.1) -> list[Month]:
    """For each month from start_t, pick the candidate with the best score on the previous train_days."""
    months = []
    prev = None
    t = start_t
    while t < end_t:
        a = max(0, t - train_days * DAY)
        scores = {c.name: window_score(equities[c.name], acct, a, t) for c in cands}
        best = max(scores, key=scores.get)
        if prev is not None and scores.get(prev, float("-inf")) >= scores[best] - stickiness:
            best = prev                                   # don't flip-flop between near-equal choices
        choice = best if scores[best] > 0 else None
        months.append(Month(t, min(t + step_days * DAY, end_t), choice, scores[best]))
        prev = choice
        t += step_days * DAY
    return months


def stitch(m: Market, sigs: dict[str, Signals], months: list[Month]) -> Signals:
    """One set of signals that follows each month's chosen candidate (nothing outside the months)."""
    out = S._blank(m, "learned", max(s.max_hold for s in sigs.values()))
    for mo in months:
        if mo.choice is None:
            out.exit_long[mo.start_t] = True                    # sitting out: close what's held
            out.exit_short[mo.start_t] = True
            continue
        s = sigs[mo.choice]
        r = slice(mo.start_t, mo.end_t)
        for name in ("entry", "stop_dist", "target_dist", "trail_dist", "exit_long", "exit_short", "score"):
            getattr(out, name)[r] = getattr(s, name)[r]
    return out


# ---- 2. learning from mistakes ---------------------------------------------------------

FEATURES = ("ret24", "ret168", "ret336", "ema50_gap_atr", "atr_pct", "vol_ratio", "rsi14_h", "btc_gap_atr",
            "side")


def entry_features(m: Market, t: int, j: int, side: int) -> np.ndarray:
    F = m.feats
    c = m.c[t, j]
    atr = F["atr20_d"][t, j]
    jb = m.coins.index("BTC") if "BTC" in m.coins else 0
    btc_atr = F["atr20_d"][t, jb]
    return np.array([
        F["ret24"][t, j] * side, F["ret168"][t, j] * side, F["ret336"][t, j] * side,
        (c - F["ema50_d"][t, j]) / atr * side if atr else 0.0,
        atr / c,
        np.log(max(F["vol_ratio"][t, j], 1e-3)) if np.isfinite(F["vol_ratio"][t, j]) else 0.0,
        (F["rsi14_h"][t, j] - 50) / 50 * side,
        (m.c[t, jb] - F["ema50_d"][t, jb]) / btc_atr if btc_atr else 0.0,
        float(side),
    ])


class Logistic:
    """Plain L2-regularised logistic regression (Newton steps), with standardised inputs."""

    def __init__(self, l2: float = 1.0):
        self.l2 = l2

    def fit(self, X: np.ndarray, y: np.ndarray) -> Logistic:
        self.mu = X.mean(0)
        self.sd = X.std(0) + 1e-9
        Z = np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])
        w = np.zeros(Z.shape[1])
        reg = np.eye(len(w)) * self.l2
        reg[0, 0] = 0.0
        for _ in range(25):
            p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
            g = Z.T @ (p - y) + reg @ w
            H = Z.T @ (Z * (p * (1 - p))[:, None]) + reg
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-6:
                break
        self.w = w
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        Z = np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])
        return 1 / (1 + np.exp(-np.clip(Z @ self.w, -30, 30)))


@dataclass
class FilterMonth:
    start_t: int
    trained_on: int                    # finished trades it learned from
    skipped: int
    kept: int
    top_lessons: list[tuple[str, float]] = field(default_factory=list)


def learn_mistakes(m: Market, sig: Signals, trades: list[dict], start_t: int, end_t: int, step_days: int = 30,
                   min_trades: int = 60, skip_share: float = 0.3) -> tuple[Signals, list[FilterMonth]]:
    """Copy of sig where, month by month, entries that look like past losers are removed.
    `trades` are the outcomes of taking sig's entries (from an unfiltered run)."""
    idx = {c: j for j, c in enumerate(m.coins)}
    rows = [(t["open_t"], t["close_t"], idx[t["coin"]], t["side"], t["pnl"]) for t in trades]
    X_all = np.array([entry_features(m, o, j, s) for o, _, j, s, _ in rows]) if rows else np.zeros((0, len(FEATURES)))
    y_all = np.array([1.0 if p > 0 else 0.0 for *_, p in rows])
    closed = np.array([c for _, c, *_ in rows])
    out = replace(sig, entry=sig.entry.copy())
    log = []
    t = start_t
    while t < end_t:
        t1 = min(t + step_days * DAY, end_t)
        known = closed < t
        cand = np.argwhere(sig.entry[t:t1] != 0)
        if known.sum() < min_trades or len(cand) == 0:
            log.append(FilterMonth(t, int(known.sum()), 0, len(cand)))
            t = t1
            continue
        X, y = X_all[known], y_all[known]
        ok = np.isfinite(X).all(1)
        model = Logistic().fit(X[ok], y[ok])
        seen = model.predict(X[ok])
        if seen.std() < 1e-3:                             # the model can't tell trades apart: skip nothing
            log.append(FilterMonth(t, int(known.sum()), 0, len(cand)))
            t = t1
            continue
        cut = np.quantile(seen, skip_share)
        base = y[ok].mean()
        skipped = 0
        for dt, j in cand:
            tt = t + dt
            f = entry_features(m, tt, j, int(sig.entry[tt, j]))
            p = model.predict(f[None])[0] if np.isfinite(f).all() else 1.0
            if p <= cut and p < base:                     # looks like past losers, and worse than average
                out.entry[tt, j] = 0
                skipped += 1
        lessons = sorted(zip(FEATURES, model.w[1:]), key=lambda kv: -abs(kv[1]))[:3]
        log.append(FilterMonth(t, int(known.sum()), skipped, len(cand) - skipped, lessons))
        t = t1
    return out, log


def run_unbounded(m: Market, sig: Signals, rules: PropRules, start_t: int, end_t: int | None = None,
                  risk_pct: float = 1.0, max_positions: int = 4):
    cfg = SimConfig(risk_pct=risk_pct, max_positions=max_positions, max_same_side=max_positions,
                    daily_brake_pct=None, enforce_rules=False)
    return simulate(m, sig, rules, cfg, start_t, end_t)
