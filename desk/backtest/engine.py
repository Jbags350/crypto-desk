"""The backtest engine: replays historical candles through the real desk code.

Reused exactly as the live bot uses them:
  - scanner rules     (indicator_frame + detect_setups), or the daily trend rule
  - Risk Manager      (RiskManager.check_entry / check_exit, every rule)
  - order handling    (Executor + Portfolio, on a throw-away in-memory database)
  - exit rules        (stop-loss, trailing stop, take-profit, max hold)
Replaced, because they can't be replayed honestly:
  - the AI agents     -> every signal is taken, with its suggested stop/target
  - news              -> not used
  - CoinSpot's spread -> a constant assumption (backtest.assumed_spread_pct)

Order types (config execution:):
  market entry  : fills at the NEXT candle's open, at the ask, plus slippage
  limit entry   : a buy order rests at the bid seen at the signal; it fills only
                  if a later candle trades BELOW that price (so someone really
                  sold to us) within entry_wait_minutes; otherwise it's cancelled
  market target : sells at the bid when the bid reaches the target
  limit target  : a resting sell at the target fills when the price trades
                  ABOVE it, at exactly the target
  stops always sell at the bid (plus slippage), at the open if the price
  jumped past them

No peeking into the future:
  - signals only see candles that had CLOSED
  - if one candle touched both the stop and the target, the stop came first
  - in the candle a limit buy fills, only the stop is checked (the high may
    have come before our fill)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from desk.backtest.history import resample
from desk.broker import BrokerError, Fill
from desk.clock import FakeClock
from desk.config import TIMEFRAME_SECONDS, CoinConfig, Config, ExitsConfig
from desk.db import Database
from desk.exchange.coinspot import Quote
from desk.executor import Executor
from desk.exits import max_hold, trail_activation_pct, trail_distance_pct, trailing_stop_price
from desk.models import Position
from desk.notify import MemoryNotifier
from desk.portfolio import Portfolio
from desk.risk import Controls, EntryRequest, MarketState, RiskDecision, RiskManager, daily_pause_active
from desk.scanner import VIEW_COLUMNS, detect_setups, indicator_frame, view_from_values
from desk.strategy import Signal, apply_strategy
from desk.trend import TREND_COLUMNS, trend_frame, trend_signal

MODE = "backtest"
DAY = 86400


def _ns(t) -> int:
    """Nanoseconds since 1970 for a datetime/Timestamp, whatever unit pandas chose."""
    return int(pd.Timestamp(t).as_unit("ns").value)


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------

class BacktestBroker:
    """Fills at a bid/ask set by the engine, plus slippage (market orders) and the CoinSpot fee."""

    mode = MODE

    def __init__(self, fee_pct: float, slippage_pct: float):
        self.fee_rate = fee_pct / 100
        self.slip = slippage_pct / 100
        self.quotes: dict[str, tuple[float, float, bool]] = {}

    def set_quote(self, coin: str, bid: float, ask: float, exact: bool = False) -> None:
        """exact=True: a limit order filling at its own price (no slippage)."""
        self.quotes[coin] = (bid, ask, exact)

    def buy(self, coin: str, qty: float, max_price: float | None = None) -> Fill:
        _, ask, exact = self.quotes[coin]
        price = ask if exact else ask * (1 + self.slip)
        if max_price is not None and price > max_price * (1 + 1e-12):
            raise BrokerError(f"price moved: {price:.8g} > limit {max_price:.8g}")
        return self._fill(coin, "buy", qty, price)

    def sell(self, coin: str, qty: float) -> Fill:
        bid, _, exact = self.quotes[coin]
        return self._fill(coin, "sell", qty, bid if exact else bid * (1 - self.slip))

    def _fill(self, coin, side, qty, price) -> Fill:
        notional = qty * price
        return Fill(coin, side, qty, qty, price, notional, notional * self.fee_rate, "backtest fill")


# ---------------------------------------------------------------------------
# Exits inside one candle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BarExit:
    reason: str | None          # None = keep holding
    price: float | None         # exit price
    highest: float              # updated highest bid (for the next candle)
    trailing: bool
    exact: bool = False         # True = a resting limit order filled at exactly this price


def bar_exit(pos: Position, o: float, h: float, l: float, c: float, bar_close: datetime,
             cfg: ExitsConfig, half_spread: float, *, tp_limit: bool = False, stop_only: bool = False) -> BarExit:
    """The rules of desk.exits.check_exit, applied to a whole candle (pessimistically)."""
    ob, hb, lb, cb = (x * (1 - half_spread) for x in (o, h, l, c))
    dist = trail_distance_pct(pos, cfg)
    trail = trailing_stop_price(pos.highest_price, cfg, dist) if pos.trailing_active else None
    stop = max(pos.stop_loss, trail) if trail is not None else pos.stop_loss
    if lb <= stop:                                         # stop checked FIRST (pessimistic)
        reason = "trailing_stop" if trail is not None and trail > pos.stop_loss else "stop_loss"
        return BarExit(reason, min(ob, stop), pos.highest_price, pos.trailing_active)
    if stop_only:
        return BarExit(None, None, pos.highest_price, pos.trailing_active)
    if tp_limit:
        if h > pos.take_profit:                            # someone traded above our resting sell
            return BarExit("take_profit", pos.take_profit, pos.highest_price, pos.trailing_active, exact=True)
    elif hb >= pos.take_profit:
        return BarExit("take_profit", max(ob, pos.take_profit), pos.highest_price, pos.trailing_active)
    highest = max(pos.highest_price, hb)
    trailing = pos.trailing_active or highest >= pos.entry_price * (1 + trail_activation_pct(pos, cfg) / 100)
    if bar_close - pos.opened_at >= timedelta(hours=max_hold(pos, cfg)):
        return BarExit("max_hold", cb, highest, trailing)
    return BarExit(None, None, highest, trailing)


# ---------------------------------------------------------------------------
# Indicator lookups with no look-ahead
# ---------------------------------------------------------------------------

@dataclass
class TfSeries:
    timeframe: str
    close_ns: np.ndarray        # candle CLOSE times (int ns), sorted
    values: np.ndarray          # one row per candle

    @classmethod
    def _make(cls, df: pd.DataFrame, timeframe: str, frame: pd.DataFrame) -> TfSeries:
        # pandas 3 may store times in s/ms/us; force nanoseconds so lookups compare like with like
        close = (df.index + pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])).as_unit("ns").asi8
        return cls(timeframe, np.asarray(close, dtype=np.int64), frame.to_numpy(dtype=float))

    @classmethod
    def build(cls, base: pd.DataFrame, timeframe: str, config: Config) -> TfSeries:
        df = resample(base, timeframe)
        return cls._make(df, timeframe, indicator_frame(df, config.scanner)[VIEW_COLUMNS])

    @classmethod
    def build_trend(cls, base: pd.DataFrame, config: Config) -> TfSeries:
        df = resample(base, "1d")
        return cls._make(df, "1d", trend_frame(df, config.strategy.trend)[TREND_COLUMNS])

    def latest_closed(self, t: datetime) -> int:
        """Row of the newest candle that had closed by time t (-1 if none). Never a later one."""
        return int(np.searchsorted(self.close_ns, _ns(t), side="right")) - 1

    def row_at(self, t: datetime):
        i = self.latest_closed(t)
        if i < 0:
            return None, None
        return self.values[i], pd.Timestamp(int(self.close_ns[i]), unit="ns", tz="UTC").to_pydatetime()

    def view_at(self, t: datetime):
        row, close_time = self.row_at(t)
        if row is None:
            return None, None
        return view_from_values(row, self.timeframe, close_time), close_time


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    start: datetime
    end: datetime
    coins: list[str]
    starting_balance: float
    equity: pd.Series                       # equity at every candle close
    hold: pd.Series                         # buy-and-hold BTC, same timestamps
    exposure: pd.Series                     # share of equity in open positions (0-1)
    trades: pd.DataFrame                    # one row per closed position
    fees_aud: float
    setups_found: Counter = field(default_factory=Counter)
    risk_rejections: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    daily_limit_days: int = 0
    open_at_end: int = 0
    in_market_pct: float = 0.0
    settings: dict = field(default_factory=dict)


@dataclass
class RestingBuy:
    signal: Signal
    limit: float
    decision: RiskDecision
    expires: datetime


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class Backtester:
    def __init__(self, config: Config, candles: dict[str, pd.DataFrame], coins: list[CoinConfig],
                 start: datetime, end: datetime, *, base_tf: str = "5m", spread_pct: float | None = None,
                 slippage_pct: float | None = None, confidence: float | None = None, benchmark: str = "BTC"):
        self.config = apply_strategy(config)
        cfg = self.config
        self.mode = cfg.strategy.mode
        self.coins = coins
        self.start = start
        self.end = end
        bt = cfg.backtest
        self.spread_pct = bt.assumed_spread_pct if spread_pct is None else spread_pct
        self.slippage_pct = bt.slippage_pct if slippage_pct is None else slippage_pct
        self.confidence = bt.entry_confidence if confidence is None else confidence
        self.half = self.spread_pct / 200
        self.benchmark = benchmark
        self.base_tf = base_tf
        self.base_step = timedelta(seconds=TIMEFRAME_SECONDS[base_tf])
        # one time unit everywhere (pandas 3 may mix s/ms/us/ns), so lookups and alignment are exact
        self.candles = {k: v.set_axis(pd.DatetimeIndex(v.index).as_unit("ns")) for k, v in candles.items()}
        sc = cfg.scanner
        self.tfs = (sc.trend_timeframe, sc.setup_timeframe, sc.trigger_timeframe)
        cycle = DAY if self.mode == "trend" else cfg.schedule.agent_cycle_minutes * 60
        self.cycle_seconds = max(cycle, int(self.base_step.total_seconds()))
        self.limit_entry = cfg.execution.entry_order == "limit"
        self.tp_limit = cfg.execution.take_profit_order == "limit"
        self.entry_wait = timedelta(minutes=cfg.execution.entry_wait_minutes)

        self.clock = FakeClock(start, tz=cfg.tz)
        self.db = Database(":memory:", self.clock)
        self.db.migrate()
        self.broker = BacktestBroker(cfg.fees.market_order_pct, self.slippage_pct)
        self.portfolio = Portfolio(self.db, self.clock, cfg, MODE)
        self.executor = Executor(self.db, self.clock, cfg, self.broker, self.portfolio, MemoryNotifier())
        self.risk = RiskManager(cfg)

    # ---- helpers ---------------------------------------------------------
    def _quote(self, coin: str, price: float, when: datetime) -> Quote:
        return Quote(coin, price * (1 - self.half), price * (1 + self.half), price, when)

    def _signals_at(self, coin: str, series: dict, t: datetime) -> tuple[list[Signal], str | None]:
        lag = self.config.data.max_candle_lag_multiple
        if self.mode == "trend":
            row, closed = series["1d"].row_at(t)
            if row is None:
                return [], "warming up"
            if (t - closed).total_seconds() > DAY * lag:
                return [], "data gap"
            sig = trend_signal(coin, row, self.config.strategy.trend)
            return ([sig] if sig else []), None
        views = []
        for tf in self.tfs:
            v, closed = series[tf].view_at(t)
            if v is None:
                return [], "warming up"
            if (t - closed).total_seconds() > TIMEFRAME_SECONDS[tf] * lag:
                return [], "data gap"
            views.append(v)
        setups = detect_setups(*views, self.config.scanner)
        return [Signal(coin, s.kind, s.suggested_stop_pct, s.suggested_target_pct, tuple(s.reasons))
                for s in setups], None

    def _state(self, quotes: dict[str, Quote], resting: list[RestingBuy]):
        """Portfolio state with resting limit buys counted as if filled: they hold a
        position slot, today's trade count and the cash they'd spend."""
        st = self.portfolio.state(quotes)
        if not resting:
            return st
        placeholders = tuple(Position(r.signal.coin, r.decision.qty, r.limit, r.decision.stop_loss_aud,
                                      r.decision.take_profit_aud, self.clock.now(), r.limit) for r in resting)
        reserved = sum(r.decision.notional_aud + r.decision.fee_aud for r in resting)
        return st.__class__(equity_aud=st.equity_aud, cash_aud=max(0.0, st.cash_aud - reserved),
                            day_start_equity_aud=st.day_start_equity_aud,
                            open_positions=st.open_positions + placeholders,
                            entries_today=st.entries_today + len(resting), live_exposure_aud=st.live_exposure_aud)

    def _gate(self, st, daily_paused: bool) -> str | None:
        r = self.config.risk
        if daily_paused or self.risk.daily_loss_hit(st):
            return "daily loss limit"
        if len(st.open_positions) >= r.max_open_positions:
            return "max open positions"
        if st.entries_today >= r.max_entries_per_day:
            return "max trades per day"
        return None

    def _decide(self, sig: Signal, q: Quote, state, daily_paused: bool, entry_price: float | None,
                rejections: Counter, skipped: Counter) -> RiskDecision | None:
        gate = self._gate(state, daily_paused)
        if gate:
            skipped[gate] += 1
            return None
        base = entry_price if entry_price is not None else q.ask
        req = EntryRequest(sig.coin, base, base * (1 - sig.stop_pct / 100), base * (1 + sig.target_pct / 100),
                           self.confidence, "BUY")
        d = self.risk.check_entry(req, state, MarketState(sig.coin, q.bid, q.ask, 0.0),
                                  Controls(daily_loss_paused=daily_paused), entry_price=entry_price)
        if not d.approved:
            for c in d.failures:
                rejections[c.name] += 1
            return None
        return d

    def _open(self, d: RiskDecision, sig: Signal):
        return self.executor.open_position(d, trail_pct=sig.trail_pct, max_hold_hours=sig.max_hold_hours,
                                           strategy=self.mode)

    # ---- run -------------------------------------------------------------
    def run(self, progress=None) -> BacktestResult:
        cfg = self.config
        codes = [c.code for c in self.coins]
        if self.mode == "trend":
            series = {c: {"1d": TfSeries.build_trend(self.candles[c], cfg)} for c in codes}
        else:
            series = {c: {tf: TfSeries.build(self.candles[c], tf, cfg) for tf in self.tfs} for c in codes}
        grid = pd.date_range(self.start, self.end - self.base_step, freq=self.base_step, tz="UTC").as_unit("ns")
        bars = {c: self.candles[c].reindex(grid)[["open", "high", "low", "close"]].to_numpy(dtype=float)
                for c in codes}
        bench = self.candles[self.benchmark].reindex(grid)[["open", "close"]].ffill().to_numpy(dtype=float)

        setups_found, rejections, skipped = Counter(), Counter(), Counter()
        pending: list[Signal] = []            # market entries to fill at the next open
        resting: list[RestingBuy] = []        # limit buys waiting for a seller
        last_price = {c: np.nan for c in codes}
        equity_vals = np.empty(len(grid))
        exposure_vals = np.empty(len(grid))
        in_market = 0
        daily_paused_on = None
        daily_days = set()
        n = len(grid)

        for i, t_open in enumerate(grid):
            t_open = t_open.to_pydatetime()
            t_close = t_open + self.base_step
            self.clock.set(t_open)
            paused = daily_pause_active(daily_paused_on, self.clock.local_date())
            opens = {c: self._quote(c, bars[c][i][0], t_open) for c in codes if not np.isnan(bars[c][i][0])}
            fresh: set[int] = set()

            # 1a) market entries decided at the previous close fill at this candle's OPEN
            for sig in pending:
                q = opens.get(sig.coin)
                if q is None:
                    skipped["no price at entry"] += 1
                    continue
                self.broker.set_quote(sig.coin, q.bid, q.ask)
                d = self._decide(sig, q, self._state(opens, resting), paused, None, rejections, skipped)
                if d:
                    self._open(d, sig)
            pending = []

            # 1b) resting limit buys: filled only if this candle traded below our price
            for order in list(resting):
                o, h, l, c = bars[order.signal.coin][i]
                if t_open >= order.expires:
                    resting.remove(order)
                    skipped["limit buy not filled (cancelled)"] += 1
                    continue
                if np.isnan(o) or not (l < order.limit):
                    continue
                resting.remove(order)
                self.broker.set_quote(order.signal.coin, order.limit, order.limit, exact=True)
                pos = self._open(order.decision, order.signal)
                if pos:
                    fresh.add(pos.id)

            # 2) exits during this candle
            for pos in self.portfolio.open_positions():
                o, h, l, c = bars[pos.coin][i]
                if np.isnan(o):
                    continue
                ex = bar_exit(pos, o, h, l, c, t_close, cfg.exits, self.half, tp_limit=self.tp_limit,
                              stop_only=pos.id in fresh)
                if ex.reason is None:
                    if ex.highest != pos.highest_price or ex.trailing != pos.trailing_active:
                        self.portfolio.update_exit_tracking(pos.id, ex.highest, ex.trailing)
                    continue
                self.clock.set(t_close if ex.reason == "max_hold" else t_open)
                self.broker.set_quote(pos.coin, ex.price, ex.price, exact=ex.exact)
                d = self.risk.check_exit(pos, pos.qty, MarketState(pos.coin, ex.price, ex.price, 0.0),
                                         reason=ex.reason)
                if d.approved:
                    self.executor.close_position(pos, pos.qty, ex.reason)

            # 3) candle close: mark to market, money in use, daily loss limit
            self.clock.set(t_close)
            closes = {}
            for c_ in codes:
                px = bars[c_][i][3]
                if not np.isnan(px):
                    last_price[c_] = px
                if not np.isnan(last_price[c_]):
                    closes[c_] = self._quote(c_, last_price[c_], t_close)
            state = self.portfolio.state(closes)
            equity_vals[i] = state.equity_aud
            exposure_vals[i] = (state.equity_aud - state.cash_aud) / state.equity_aud if state.equity_aud else 0.0
            if state.open_positions:
                in_market += 1
            today = self.clock.local_date()
            if self.risk.daily_loss_hit(state) and not daily_pause_active(daily_paused_on, today):
                daily_paused_on = today
                daily_days.add(today)
            paused = daily_pause_active(daily_paused_on, today)

            # 4) signal check at each cycle close (15 min; daily for trend mode)
            if int(t_close.timestamp()) % self.cycle_seconds == 0:
                held = {p.coin for p in state.open_positions}
                waiting = {s.coin for s in pending} | {r.signal.coin for r in resting}
                for c_ in codes:
                    sigs, why = self._signals_at(c_, series[c_], t_close)
                    if why:
                        continue
                    for s in sigs:
                        setups_found[s.kind] += 1
                    if not sigs or c_ in held or c_ in waiting or c_ not in closes:
                        continue
                    sig = sigs[0]
                    if not self.limit_entry:
                        pending.append(sig)
                        continue
                    q = closes[c_]
                    d = self._decide(sig, q, self._state(closes, resting), paused, q.bid, rejections, skipped)
                    if d:
                        resting.append(RestingBuy(sig, q.bid, d, t_close + self.entry_wait))
            if progress and i % max(1, n // 20) == 0:
                progress(i, n)

        if progress:
            progress(n, n)
        return self._result(grid, equity_vals, exposure_vals, bench, setups_found, rejections, skipped,
                            len(daily_days), in_market / max(1, n))

    def _result(self, grid, equity_vals, exposure_vals, bench, setups_found, rejections, skipped, daily_days,
                in_market) -> BacktestResult:
        start_bal = self.portfolio.starting_balance()
        fee = self.config.fees.market_order_pct / 100
        slip = self.slippage_pct / 100
        first_open = bench[0][0]
        hold_qty = start_bal / (first_open * (1 + self.half) * (1 + slip) * (1 + fee))
        hold_vals = hold_qty * bench[:, 1] * (1 - self.half) * (1 - slip) * (1 - fee)

        rows = self.db.query("SELECT * FROM positions WHERE mode = ? AND status = 'closed' ORDER BY id", (MODE,))
        recs = []
        for r in rows:
            cost = r["initial_qty"] * r["entry_price"]
            recs.append({
                "coin": r["coin"], "opened": r["opened_at"], "closed": r["closed_at"],
                "entry_price": r["entry_price"], "exit_price": r["exit_price"], "qty": r["initial_qty"],
                "exit_reason": r["exit_reason"], "pnl_aud": r["realized_pnl_aud"],
                "pnl_pct": r["realized_pnl_aud"] / cost * 100 if cost else 0.0,
            })
        trades = pd.DataFrame(recs, columns=["coin", "opened", "closed", "entry_price", "exit_price", "qty",
                                             "exit_reason", "pnl_aud", "pnl_pct"])
        fees = self.db.query_one("SELECT COALESCE(SUM(fee_aud), 0) FROM trades WHERE mode = ?", (MODE,))[0]
        index = grid + self.base_step
        ex = self.config.execution
        return BacktestResult(
            start=self.start, end=self.end, coins=[c.code for c in self.coins], starting_balance=start_bal,
            equity=pd.Series(equity_vals, index=index, name="strategy"),
            hold=pd.Series(hold_vals, index=index, name=f"hold {self.benchmark}"),
            exposure=pd.Series(exposure_vals, index=index, name="exposure"),
            trades=trades, fees_aud=float(fees), setups_found=setups_found, risk_rejections=rejections,
            skipped=skipped, daily_limit_days=daily_days,
            open_at_end=len(self.portfolio.open_positions()), in_market_pct=in_market * 100,
            settings={"spread_pct": self.spread_pct, "slippage_pct": self.slippage_pct,
                      "confidence": self.confidence, "fee_pct": self.config.fees.market_order_pct,
                      "mode": self.mode, "entry_order": ex.entry_order, "take_profit_order": ex.take_profit_order,
                      "base_timeframe": self.base_tf},
        )
