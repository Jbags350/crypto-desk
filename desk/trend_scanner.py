"""Trend scanner for the live/paper desk (strategy.mode: trend).

Once a day, after the daily candle closes (00:00 UTC = 10:00 Brisbane), it
reads each coin's daily candles and applies the same rule the backtester
tested (desk/trend.py). It also runs the same data-safety checks as the
intraday scanner: fresh CoinSpot price, fresh candles, CoinSpot in line with
the global price.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from desk.clock import Clock
from desk.config import CoinConfig, Config
from desk.data.candles import CandleError, CandleFeed
from desk.data.market import MarketSnapshot, divergence_pct, to_aud
from desk.strategy import Signal
from desk.trend import trend_frame, trend_signal

log = logging.getLogger("desk.trend_scanner")

DAY_SECONDS = 86400


@dataclass
class TrendScan:
    coin: str
    at: datetime
    quote_at: datetime | None = None
    price_aud: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None
    global_divergence_pct: float | None = None
    candle_close_time: datetime | None = None     # when the newest daily candle closed
    daily_close: float | None = None             # global (USDT) prices: compare in %
    prior_high: float | None = None
    ema: float | None = None
    atr_pct: float | None = None
    signal: Signal | None = None
    problems: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)       # kept for the shared exit-review code
    setups: list = field(default_factory=list)
    volume_spikes: list[str] = field(default_factory=list)
    views: dict = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return not self.problems

    @property
    def flagged(self) -> bool:
        return self.signal is not None and self.healthy

    @property
    def trend(self) -> str:
        if self.daily_close is None or self.ema is None:
            return "unknown"
        return "up" if self.daily_close > self.ema else "down"

    def levels(self) -> tuple[float, float, float] | None:
        """(entry, stop, take-profit) from the rule, at today's CoinSpot ask."""
        if self.signal is None or self.ask is None:
            return None
        entry = self.ask
        return (entry, entry * (1 - self.signal.stop_pct / 100), entry * (1 + self.signal.target_pct / 100))


class TrendScanner:
    def __init__(self, config: Config, candles: CandleFeed, clock: Clock):
        self.config = config
        self.candles = candles
        self.clock = clock

    def scan(self, snapshot: MarketSnapshot, coins: list[CoinConfig] | None = None) -> list[TrendScan]:
        out = []
        for c in coins or self.config.coins:
            try:
                out.append(self.scan_coin(c, snapshot))
            except Exception as exc:     # one bad coin must never stop the others
                log.exception("Trend scan of %s failed", c.code)
                out.append(TrendScan(coin=c.code, at=self.clock.now(), problems=[f"trend scanner error: {exc}"]))
        return out

    def scan_coin(self, coin: CoinConfig, snapshot: MarketSnapshot) -> TrendScan:
        cfg, t = self.config, self.config.strategy.trend
        now = self.clock.now()
        r = TrendScan(coin=coin.code, at=now)
        q = snapshot.quotes.get(coin.code)
        if q is None:
            r.problems.append("no CoinSpot price")
        else:
            r.price_aud, r.bid, r.ask, r.spread_pct, r.quote_at = q.mid, q.bid, q.ask, round(q.spread_pct, 4), q.fetched_at
        if snapshot.is_stale(now, cfg.data.max_price_age_seconds):
            r.problems.append(f"CoinSpot price is {snapshot.age_seconds(now):.0f}s old")

        needed = max(t.breakout_days, t.trend_ema_days, t.atr_days) * 3
        try:
            cs = self.candles.get(coin, "1d", max(needed, cfg.data.candles_per_timeframe))
        except CandleError as exc:
            r.problems.append(f"daily candles unavailable ({exc})")
            return r
        if cs.lag_seconds(now) > DAY_SECONDS * cfg.data.max_candle_lag_multiple:
            r.problems.append(f"daily candles are stale ({cs.lag_seconds(now) / 3600:.0f}h since the last close)")
        f = trend_frame(cs.df, t)
        row = f.iloc[-1]
        r.candle_close_time = cs.last_close_time
        r.daily_close, r.prior_high, r.ema, r.atr_pct = (
            float(row["close"]), _f(row["prior_high"]), _f(row["ema"]), _f(row["atr_pct"]))
        if r.ema is None or r.prior_high is None or r.atr_pct is None:
            r.problems.append("not enough daily history for the trend rule")
        r.signal = trend_signal(coin.code, row, t)

        if q is not None:
            try:
                price, quote_ccy, _ = self.candles.spot_price(coin)
                g_aud = to_aud(price, quote_ccy, snapshot.usdt_aud)
                if g_aud is None:
                    r.problems.append("can't convert the global price to AUD (no USDT price)")
                else:
                    r.global_divergence_pct = round(divergence_pct(q.mid, g_aud), 4)
                    if abs(r.global_divergence_pct) > cfg.data.max_price_divergence_pct:
                        r.problems.append(f"CoinSpot price is {r.global_divergence_pct:+.2f}% from the global price")
            except CandleError as exc:
                r.problems.append(f"can't check CoinSpot against the global price ({exc})")
        return r


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else x
