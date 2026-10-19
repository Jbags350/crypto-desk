"""Market Scanner (pure code, no AI).

For each coin it works out indicators on three timeframes, checks the data
is trustworthy, and flags trade "setups". The AI agents only run for coins
the scanner flags, which keeps AI costs down.

Timeframe roles (set in config.yaml -> scanner):
  trend   (default 1h)  is the coin in an uptrend?
  setup   (default 15m) where the trade idea comes from
  trigger (default 5m)  is momentum turning in our favour right now?

The bot only buys (CoinSpot is spot-only, no short selling), so every setup
is a reason to BUY. Bearish signs become "alerts" for open positions, which
the Exit Agent looks at.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd

from desk import indicators as ind
from desk.clock import Clock, iso
from desk.config import CoinConfig, Config, ScannerConfig
from desk.data.candles import CandleError, CandleFeed, CandleSet
from desk.data.market import MarketSnapshot, divergence_pct, to_aud
from desk.db import Database

log = logging.getLogger("desk.scanner")


def _f(x) -> float | None:
    """Float, or None if missing / NaN."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


@dataclass(frozen=True)
class TfView:
    """The latest indicator values for one timeframe."""

    timeframe: str
    candle_close_time: str
    close: float
    prev_close: float | None
    ema_fast: float | None
    ema_mid: float | None
    ema_slow: float | None
    ema_trend: float | None
    rsi: float | None
    prev_rsi: float | None
    min_rsi_recent: float | None     # lowest RSI over the last 3 candles
    macd: float | None
    macd_signal: float | None
    hist: float | None
    prev_hist: float | None
    atr: float | None
    atr_pct: float | None            # ATR as % of price
    vol_ratio: float | None          # volume vs recent average
    breakout_level: float | None     # highest high of the previous N candles


VIEW_COLUMNS = ["close", "prev_close", "ema_fast", "ema_mid", "ema_slow", "ema_trend", "rsi", "prev_rsi",
                "min_rsi_recent", "macd", "macd_signal", "hist", "prev_hist", "atr", "atr_pct", "vol_ratio",
                "breakout_level"]


def indicator_frame(df: pd.DataFrame, sc: ScannerConfig) -> pd.DataFrame:
    """Every indicator the scanner uses, for EVERY candle (one row per candle).
    The live scanner reads the last row; the backtester reads the row that was
    the latest closed candle at each moment of the replay. Same code, same numbers."""
    close = df["close"]
    r = ind.rsi(close, sc.rsi_period)
    m = ind.macd(close, sc.macd_fast, sc.macd_slow, sc.macd_signal)
    a = ind.atr(df, sc.atr_period)
    out = pd.DataFrame(index=df.index)
    out["close"] = close
    out["prev_close"] = close.shift(1)
    out["ema_fast"] = ind.ema(close, sc.ema_fast)
    out["ema_mid"] = ind.ema(close, sc.ema_mid)
    out["ema_slow"] = ind.ema(close, sc.ema_slow)
    out["ema_trend"] = ind.ema(close, sc.ema_trend)
    out["rsi"] = r
    out["prev_rsi"] = r.shift(1)
    out["min_rsi_recent"] = r.rolling(3, min_periods=3).min()
    out["macd"] = m["macd"]
    out["macd_signal"] = m["signal"]
    out["hist"] = m["hist"]
    out["prev_hist"] = m["hist"].shift(1)
    out["atr"] = a
    out["atr_pct"] = a / close * 100
    out["vol_ratio"] = ind.volume_ratio(df["volume"], sc.volume_lookback)
    out["breakout_level"] = ind.highest(df["high"], sc.breakout_lookback)
    return out


def view_from_values(values, timeframe: str, close_time: datetime) -> TfView:
    """Build a TfView from one indicator_frame row (a Series, or a plain sequence in VIEW_COLUMNS order)."""
    vals = [values[c] for c in VIEW_COLUMNS] if isinstance(values, pd.Series) else list(values)
    v = dict(zip(VIEW_COLUMNS, (_f(x) for x in vals)))
    return TfView(timeframe=timeframe, candle_close_time=iso(close_time), **v)


def compute_view(cs: CandleSet, sc: ScannerConfig) -> TfView:
    return view_from_values(indicator_frame(cs.df, sc).iloc[-1], cs.timeframe, cs.last_close_time)


@dataclass
class Setup:
    kind: str                      # trend_pullback | volume_breakout | oversold_bounce
    reasons: list[str]
    suggested_stop_pct: float      # suggested stop-loss distance below entry, in %
    suggested_target_pct: float    # suggested take-profit distance above entry, in %


@dataclass
class CoinScan:
    coin: str
    at: datetime
    quote_at: datetime | None = None        # when the CoinSpot price was fetched
    price_aud: float | None = None          # CoinSpot mid price
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None
    global_divergence_pct: float | None = None
    trend: str = "unknown"                  # up | down | flat | unknown
    views: dict[str, TfView] = field(default_factory=dict)
    setups: list[Setup] = field(default_factory=list)
    suppressed_setups: list[Setup] = field(default_factory=list)   # found, but data problems block them
    alerts: list[str] = field(default_factory=list)                # bearish signs (for open positions)
    volume_spikes: list[str] = field(default_factory=list)         # timeframes with a volume spike
    problems: list[str] = field(default_factory=list)              # data problems -> no trading

    @property
    def healthy(self) -> bool:
        return not self.problems

    @property
    def flagged(self) -> bool:
        return bool(self.setups)

    def to_dict(self) -> dict:
        return {
            "coin": self.coin,
            "at": iso(self.at),
            "quote_at": iso(self.quote_at) if self.quote_at else None,
            "price_aud": self.price_aud,
            "bid": self.bid,
            "ask": self.ask,
            "spread_pct": self.spread_pct,
            "global_divergence_pct": self.global_divergence_pct,
            "trend": self.trend,
            "views": {tf: asdict(v) for tf, v in self.views.items()},
            "setups": [asdict(s) for s in self.setups],
            "suppressed_setups": [asdict(s) for s in self.suppressed_setups],
            "alerts": self.alerts,
            "volume_spikes": self.volume_spikes,
            "problems": self.problems,
        }


# ---------------------------------------------------------------------------
# Rules (pure functions, unit tested directly)
# ---------------------------------------------------------------------------

def classify_trend(t: TfView) -> str:
    if None in (t.ema_slow, t.ema_trend):
        return "unknown"
    if t.close > t.ema_slow > t.ema_trend:
        return "up"
    if t.close < t.ema_slow < t.ema_trend:
        return "down"
    return "flat"


def _rising(now: float | None, prev: float | None) -> bool:
    return now is not None and prev is not None and now > prev


def detect_setups(t: TfView, s: TfView, g: TfView, sc: ScannerConfig) -> list[Setup]:
    """t = trend timeframe view, s = setup timeframe, g = trigger timeframe."""
    found: list[Setup] = []
    trend = classify_trend(t)
    stop = (s.atr_pct or 0) * sc.stop_atr_multiple
    target = (s.atr_pct or 0) * sc.target_atr_multiple
    if not s.atr_pct:
        return found      # can't size stops without ATR

    def add(kind: str, reasons: list[str]):
        if kind in sc.setups:
            found.append(Setup(kind, reasons, round(stop, 3), round(target, 3)))

    # 1. Trend pullback: uptrend, price dipped back to the EMA, momentum turning up
    if (trend == "up" and s.min_rsi_recent is not None and s.min_rsi_recent < sc.pullback_rsi
            and _rising(s.rsi, s.prev_rsi) and s.ema_mid is not None and s.atr is not None
            and abs(s.close - s.ema_mid) <= sc.pullback_atr_band * s.atr
            and _rising(g.hist, g.prev_hist)):
        add("trend_pullback", [
            f"{t.timeframe} uptrend (price above EMA{sc.ema_slow}, which is above EMA{sc.ema_trend})",
            f"{s.timeframe} RSI dipped to {s.min_rsi_recent:.1f} and is turning up ({s.rsi:.1f})",
            f"{s.timeframe} price is at its EMA{sc.ema_mid} (within {sc.pullback_atr_band} ATR)",
            f"{g.timeframe} MACD momentum is rising",
        ])

    # 2. Volume breakout: new high on a volume spike, momentum positive, not in a downtrend
    if (s.breakout_level is not None and s.close > s.breakout_level
            and s.vol_ratio is not None and s.vol_ratio >= sc.volume_spike_ratio
            and s.hist is not None and s.hist > 0 and _rising(s.hist, s.prev_hist)
            and s.rsi is not None and s.rsi < sc.overbought_rsi
            and t.ema_slow is not None and t.close > t.ema_slow):
        add("volume_breakout", [
            f"{s.timeframe} close broke above the {sc.breakout_lookback}-candle high ({s.breakout_level:.6g})",
            f"{s.timeframe} volume is {s.vol_ratio:.1f}x normal",
            f"{s.timeframe} MACD histogram positive and rising",
            f"{s.timeframe} RSI {s.rsi:.1f} is below the overbought level {sc.overbought_rsi:g}",
            f"{t.timeframe} price above EMA{sc.ema_slow}",
        ])

    # 3. Oversold bounce: long-term uptrend, RSI bouncing out of oversold
    trigger_cross = (g.prev_rsi is not None and g.rsi is not None
                     and g.prev_rsi < sc.oversold_rsi <= g.rsi)
    setup_oversold = s.rsi is not None and s.rsi < sc.oversold_rsi and _rising(s.rsi, s.prev_rsi)
    if (t.ema_trend is not None and t.close > t.ema_trend
            and (trigger_cross or setup_oversold) and _rising(g.hist, g.prev_hist)):
        why = (f"{g.timeframe} RSI crossed back up through {sc.oversold_rsi:g} ({g.prev_rsi:.1f} -> {g.rsi:.1f})"
               if trigger_cross else f"{s.timeframe} RSI oversold ({s.rsi:.1f}) and turning up")
        add("oversold_bounce", [
            f"{t.timeframe} price above EMA{sc.ema_trend} (long-term uptrend)",
            why,
            f"{g.timeframe} MACD momentum is rising",
        ])
    return found


def position_alerts(t: TfView, s: TfView, g: TfView, sc: ScannerConfig) -> list[str]:
    """Bearish signs. Used to decide whether to ask the Exit Agent about an open position."""
    alerts = []
    if s.hist is not None and s.prev_hist is not None and s.hist < 0 <= s.prev_hist:
        alerts.append(f"{s.timeframe} MACD crossed below its signal line")
    if s.ema_mid is not None and s.close < s.ema_mid and (s.prev_close or 0) >= s.ema_mid:
        alerts.append(f"{s.timeframe} price fell below EMA{sc.ema_mid}")
    if t.ema_slow is not None and t.close < t.ema_slow:
        alerts.append(f"{t.timeframe} price below EMA{sc.ema_slow} (trend weakening)")
    if s.rsi is not None and s.rsi >= sc.overbought_rsi:
        alerts.append(f"{s.timeframe} RSI overbought ({s.rsi:.1f})")
    if (g.vol_ratio is not None and g.vol_ratio >= sc.volume_spike_ratio
            and g.prev_close is not None and g.close < g.prev_close):
        alerts.append(f"{g.timeframe} selling on a volume spike ({g.vol_ratio:.1f}x normal)")
    return alerts


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self, config: Config, candles: CandleFeed, clock: Clock, db: Database | None = None):
        self.config = config
        self.candles = candles
        self.clock = clock
        self.db = db

    def scan(self, snapshot: MarketSnapshot, coins: list[CoinConfig] | None = None,
             cycle_id: str | None = None) -> list[CoinScan]:
        results = []
        for coin in coins or self.config.coins:
            try:
                r = self.scan_coin(coin, snapshot)
            except Exception as exc:     # one bad coin must never stop the others
                log.exception("Scanning %s failed", coin.code)
                r = CoinScan(coin=coin.code, at=self.clock.now(), problems=[f"scanner error: {exc}"])
            results.append(r)
            if self.db is not None:
                self._save(r, cycle_id)
        return results

    def scan_coin(self, coin: CoinConfig, snapshot: MarketSnapshot) -> CoinScan:
        cfg, sc = self.config, self.config.scanner
        now = self.clock.now()
        r = CoinScan(coin=coin.code, at=now)

        # CoinSpot price (what we'd actually trade at)
        q = snapshot.quotes.get(coin.code)
        if q is None:
            r.problems.append("no CoinSpot price")
        else:
            r.price_aud, r.bid, r.ask, r.spread_pct = q.mid, q.bid, q.ask, round(q.spread_pct, 4)
            r.quote_at = q.fetched_at
        if snapshot.is_stale(now, cfg.data.max_price_age_seconds):
            r.problems.append(f"CoinSpot price is {snapshot.age_seconds(now):.0f}s old")

        # Candles + indicators
        for tf in cfg.data.timeframes:
            try:
                cs = self.candles.get(coin, tf, cfg.data.candles_per_timeframe)
            except CandleError as exc:
                r.problems.append(f"{tf} candles unavailable ({exc})")
                continue
            if not cs.is_fresh(now, cfg.data.max_candle_lag_multiple):
                r.problems.append(f"{tf} candles are stale ({cs.lag_seconds(now):.0f}s since last close)")
            r.views[tf] = compute_view(cs, sc)

        # Is CoinSpot's price in line with the rest of the world?
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

        t = r.views.get(sc.trend_timeframe)
        s = r.views.get(sc.setup_timeframe)
        g = r.views.get(sc.trigger_timeframe)
        r.volume_spikes = [tf for tf, v in r.views.items()
                           if v.vol_ratio is not None and v.vol_ratio >= sc.volume_spike_ratio]
        if t and s and g:
            r.trend = classify_trend(t)
            setups = detect_setups(t, s, g, sc)
            r.alerts = position_alerts(t, s, g, sc)
            if r.problems:
                r.suppressed_setups = setups
            else:
                r.setups = setups
        return r

    def _save(self, r: CoinScan, cycle_id: str | None) -> None:
        self.db.execute(
            "INSERT INTO scanner_signals(ts, cycle_id, coin, price_aud, trend, flagged, healthy, setups, alerts, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(r.at), cycle_id, r.coin, r.price_aud, r.trend, int(r.flagged), int(r.healthy),
             json.dumps([s.kind for s in r.setups]), json.dumps(r.alerts), json.dumps(r.to_dict(), default=str)),
        )
