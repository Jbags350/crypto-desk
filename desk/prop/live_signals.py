"""Today's rotation decision, from the same code the research used.

Hourly Binance candles (cached in the backtest candle cache, only new hours
are downloaded) -> desk.prop.market.build_market -> desk.prop.strategies.rotation.
Because the live bot and prop_research.bat share these functions, the bot
makes exactly the decisions that were tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from desk.config import Config
from desk.prop.market import Market, build_market, load_hourly
from desk.prop.strategies import rotation

HISTORY_DAYS = 420           # enough for a 50-day average of daily candles plus a margin


@dataclass
class CoinView:
    coin: str
    close: float
    ret: float                   # return over the lookback
    rank: int                    # 1 = strongest
    above_ema50: bool
    ema50_gap_pct: float
    atr_pct: float               # daily ATR as % of price
    change_24h_pct: float


@dataclass
class RotationDecision:
    candle_close: datetime
    bull: bool                   # regime coin above its 50-day average
    buy: dict[str, float]        # coin -> priority score
    sell: set[str]               # coins to close if held
    stop_dist: dict[str, float]  # coin -> stop distance (price units, Binance prices)
    views: dict[str, CoinView] = field(default_factory=dict)

    def stop_pct(self, coin: str) -> float:
        return self.stop_dist[coin] / self.views[coin].close


def floor_hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


class LiveSignals:
    def __init__(self, config: Config, cache_dir: Path | None = None, *, offline: bool = False, loader=None):
        self.config = config
        self.p = config.prop
        self.cache_dir = cache_dir or config.path("candle_cache")
        self.offline = offline
        self._load = loader or (lambda start, end: load_hourly(dict(self.p.price_symbols), start, end,
                                                                self.cache_dir, offline=self.offline))

    def market(self, now: datetime, grid_days: int = 30) -> Market:
        end = floor_hour(now)
        frames = self._load(end - timedelta(days=HISTORY_DAYS), end)
        frames = {k: v for k, v in frames.items() if not v.empty}
        if self.p.strategy.regime_coin not in frames:
            raise RuntimeError(f"no price history for {self.p.strategy.regime_coin}")
        return build_market(frames, end - timedelta(days=grid_days), end)

    def rotation(self, now: datetime) -> RotationDecision | None:
        """The decision for the most recent rebalance candle, or None if that candle isn't in yet."""
        st = self.p.strategy
        m = self.market(now)
        t = m.T - 1
        if m.close_hour()[t] != st.rebalance_hour_utc or not m.valid[t].any():
            return None
        sig = rotation(m, lookback=st.lookback_hours, top=st.top, stop_atr_d=st.stop_atr_days, shorts=False,
                       rebalance_hour=st.rebalance_hour_utc, regime_coin=st.regime_coin)
        F = m.feats
        ret = F[f"ret{st.lookback_hours}"][t] if f"ret{st.lookback_hours}" in F else np.full(m.N, np.nan)
        order = np.argsort(-np.nan_to_num(ret, nan=-np.inf))
        rank = {m.coins[j]: i + 1 for i, j in enumerate(order)}
        views = {}
        for j, coin in enumerate(m.coins):
            close = float(m.c[t, j])
            ema = F["ema50_d"][t, j]
            atr = F["atr20_d"][t, j]
            prev = m.c[t - 24, j] if t >= 24 else np.nan
            views[coin] = CoinView(coin, close, float(ret[j]), rank[coin], bool(close > ema),
                                   float((close / ema - 1) * 100) if np.isfinite(ema) else float("nan"),
                                   float(atr / close * 100) if np.isfinite(atr) else float("nan"),
                                   float((close / prev - 1) * 100) if np.isfinite(prev) else float("nan"))
        j_reg = m.coins.index(st.regime_coin)
        bull = bool(m.c[t, j_reg] > F["ema50_d"][t, j_reg])
        buy = {m.coins[j]: float(sig.score[t, j]) for j in range(m.N) if sig.entry[t, j] == 1}
        sell = {m.coins[j] for j in range(m.N) if sig.exit_long[t, j]}
        stop = {m.coins[j]: float(sig.stop_dist[t, j]) for j in range(m.N) if np.isfinite(sig.stop_dist[t, j])}
        close_time = (m.index[t] + pd.Timedelta(hours=1)).to_pydatetime()
        return RotationDecision(close_time, bull, buy, sell, stop, views)

    def last_closes(self, now: datetime) -> tuple[datetime, dict[str, float]]:
        """Close of the most recent finished hourly candle for each coin."""
        end = floor_hour(now)
        frames = self._load(end - timedelta(hours=6), end)
        out = {}
        last = None
        for coin, df in frames.items():
            if not df.empty:
                out[coin] = float(df["close"].iloc[-1])
                ts = (df.index[-1] + pd.Timedelta(hours=1)).to_pydatetime()
                last = ts if last is None else max(last, ts)
        return last, out
