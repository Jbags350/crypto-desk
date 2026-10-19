"""Loads and validates config/config.yaml and the secrets in .env.

Every setting is checked on start-up. A typo in a setting name or an
out-of-range value stops the bot with a clear message instead of
silently trading with a wrong limit.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


class ConfigError(Exception):
    """The config file or .env is missing or invalid."""


class _Strict(BaseModel):
    # extra="forbid" turns a misspelled setting name into an error
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoinConfig(_Strict):
    code: str
    binance: str
    kraken: str | None = None
    news_aliases: list[str] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,10}", v):
            raise ValueError(f"'{v}' is not a valid coin code")
        return v

    @property
    def coinspot(self) -> str:
        """CoinSpot's public API uses lower-case coin codes."""
        return self.code.lower()


class ScheduleConfig(_Strict):
    fast_check_seconds: int = Field(ge=10, le=600)
    agent_cycle_minutes: int = Field(ge=1, le=240)
    cycle_delay_seconds: int = Field(ge=0, le=300)
    news_refresh_minutes: int = Field(ge=1, le=240)
    daily_summary_time: str
    keep_pc_awake: bool = True

    @field_validator("daily_summary_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("must be HH:MM in 24-hour time, e.g. \"23:55\"")
        return v


class DataConfig(_Strict):
    candle_providers: list[Literal["binance", "kraken"]] = Field(min_length=1)
    timeframes: list[str] = Field(min_length=1)
    candles_per_timeframe: int = Field(ge=50, le=1000)
    max_price_age_seconds: int = Field(ge=10, le=3600)
    max_candle_lag_multiple: float = Field(ge=1.0, le=10.0)
    max_price_divergence_pct: float = Field(gt=0, le=20)
    api_error_streak_limit: int = Field(ge=1, le=100)

    @field_validator("timeframes")
    @classmethod
    def _known_timeframes(cls, v: list[str]) -> list[str]:
        bad = [tf for tf in v if tf not in TIMEFRAME_SECONDS]
        if bad:
            raise ValueError(f"unknown timeframe(s) {bad}; allowed: {list(TIMEFRAME_SECONDS)}")
        return v


SETUP_NAMES = ("trend_pullback", "volume_breakout", "oversold_bounce")


class SwingConfig(_Strict):
    trend_timeframe: str
    setup_timeframe: str
    trigger_timeframe: str
    stop_atr_multiple: float = Field(gt=0, le=20)
    target_atr_multiple: float = Field(gt=0, le=50)
    trailing_activate_pct: float = Field(ge=0, le=100)
    trailing_distance_pct: float = Field(gt=0, le=50)
    max_hold_hours: float = Field(gt=0, le=24 * 60)


class TrendConfig(_Strict):
    breakout_days: int = Field(ge=2, le=400)
    trend_ema_days: int = Field(ge=2, le=400)
    atr_days: int = Field(ge=2, le=200)
    stop_atr_multiple: float = Field(gt=0, le=20)
    max_hold_days: float = Field(gt=0, le=3650)
    max_stop_distance_pct: float = Field(gt=0, le=50)


class StrategyConfig(_Strict):
    mode: Literal["day", "swing", "trend"]
    swing: SwingConfig
    trend: TrendConfig


class ExecutionConfig(_Strict):
    entry_order: Literal["market", "limit"]
    entry_wait_minutes: int = Field(ge=1, le=1440)
    take_profit_order: Literal["market", "limit"]


class ScannerConfig(_Strict):
    trend_timeframe: str
    setup_timeframe: str
    trigger_timeframe: str
    setups: list[Literal["trend_pullback", "volume_breakout", "oversold_bounce"]]
    rsi_period: int = Field(ge=2, le=100)
    ema_fast: int = Field(ge=2, le=500)
    ema_mid: int = Field(ge=2, le=500)
    ema_slow: int = Field(ge=2, le=500)
    ema_trend: int = Field(ge=2, le=500)
    macd_fast: int = Field(ge=2, le=200)
    macd_slow: int = Field(ge=3, le=400)
    macd_signal: int = Field(ge=2, le=200)
    atr_period: int = Field(ge=2, le=200)
    volume_lookback: int = Field(ge=3, le=500)
    volume_spike_ratio: float = Field(gt=1.0, le=100)
    pullback_rsi: float = Field(gt=0, lt=100)
    oversold_rsi: float = Field(gt=0, lt=100)
    overbought_rsi: float = Field(gt=0, lt=100)
    breakout_lookback: int = Field(ge=3, le=500)
    pullback_atr_band: float = Field(gt=0, le=10)
    stop_atr_multiple: float = Field(gt=0, le=20)
    target_atr_multiple: float = Field(gt=0, le=50)

    @model_validator(mode="after")
    def _ordered(self) -> ScannerConfig:
        if not (self.ema_fast < self.ema_mid < self.ema_slow < self.ema_trend):
            raise ValueError("EMAs must be in order: ema_fast < ema_mid < ema_slow < ema_trend")
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be smaller than macd_slow")
        if not (self.oversold_rsi < self.pullback_rsi < self.overbought_rsi):
            raise ValueError("RSI levels must be in order: oversold_rsi < pullback_rsi < overbought_rsi")
        return self


class NewsConfig(_Strict):
    rss_feeds: list[str]
    cryptopanic_url: str
    max_age_hours: float = Field(gt=0, le=168)
    max_headlines_per_coin: int = Field(ge=1, le=100)
    score_cache_minutes: float = Field(ge=0, le=1440)
    event_keywords: dict[str, list[str]]
    exchange_names: list[str]

    @field_validator("rss_feeds")
    @classmethod
    def _urls(cls, v: list[str]) -> list[str]:
        bad = [u for u in v if not u.startswith(("http://", "https://"))]
        if bad:
            raise ValueError(f"feed addresses must start with https:// ({bad})")
        return v


class RiskConfig(_Strict):
    max_position_pct: float = Field(gt=0, le=100)
    max_open_positions: int = Field(ge=1, le=50)
    daily_loss_limit_pct: float = Field(gt=0, le=100)
    max_entries_per_day: int = Field(ge=0, le=500)
    max_stop_distance_pct: float = Field(gt=0, le=50)
    min_reward_risk: float = Field(gt=0, le=20)
    min_edge_multiple: float = Field(ge=1.0, le=50)
    min_confidence: int = Field(ge=0, le=100)
    min_order_aud: float = Field(gt=0)
    major_event_block_hours: float = Field(ge=0, le=168)
    max_entry_gap_pct: float = Field(gt=0, le=10)


class FeesConfig(_Strict):
    market_order_pct: float = Field(ge=0, le=5)
    slippage_estimate_pct: float = Field(ge=0, le=5)


class ExitsConfig(_Strict):
    trailing_activate_pct: float = Field(ge=0, le=100)
    trailing_distance_pct: float = Field(gt=0, le=50)
    max_hold_hours: float = Field(gt=0, le=24 * 30)
    tighten_min_gap_pct: float = Field(ge=0, le=20)


class PaperConfig(_Strict):
    starting_balance_aud: float = Field(gt=0)
    extra_slippage_pct: float = Field(ge=0, le=5)


class BacktestConfig(_Strict):
    days: int = Field(ge=7, le=3650)
    assumed_spread_pct: float = Field(ge=0, le=5)
    slippage_pct: float = Field(ge=0, le=5)
    entry_confidence: float = Field(ge=0, le=100)
    candle_cache: str


class LLMConfig(_Strict):
    model: str = Field(min_length=3)
    effort: Literal["low", "medium", "high"]
    max_tokens: int = Field(ge=256, le=64000)
    timeout_seconds: float = Field(ge=5, le=600)
    daily_spend_cap_usd: float = Field(ge=0)
    price_input_per_mtok_usd: float = Field(ge=0)
    price_output_per_mtok_usd: float = Field(ge=0)


class LiveConfig(_Strict):
    max_capital_aud: float = Field(gt=0)
    entry_fill_timeout_seconds: int = Field(ge=5, le=3600)
    exit_reprice_seconds: int = Field(ge=5, le=600)
    max_exit_slippage_pct: float = Field(gt=0, le=50)


class WatchdogConfig(_Strict):
    alert_after_minutes: float = Field(ge=2, le=1440)
    repeat_alert_minutes: float = Field(ge=5, le=10080)
    check_every_seconds: int = Field(ge=10, le=3600)


class PropStrategyConfig(_Strict):
    lookback_hours: int = Field(ge=24, le=2000)        # "strongest" = return over this many hours
    top: int = Field(ge=1, le=10)                      # hold this many of the strongest coins
    stop_atr_days: float = Field(gt=0, le=10)          # stop / trailing stop distance in daily ATRs
    regime_coin: str                                   # only buy while this coin is above its 50-day average
    rebalance_hour_utc: int = Field(ge=0, le=23)       # the daily decision, at this candle close


class PropRiskConfig(_Strict):
    risk_pct: float = Field(gt=0, le=5)                # % of the account size lost if a stop is hit
    max_positions: int = Field(ge=1, le=20)
    max_same_side: int = Field(ge=1, le=20)
    daily_brake_pct: float = Field(gt=0, le=20)        # close everything and rest for the day at this loss
    floor_scaling: bool                                # trade smaller as the account nears the max-loss floor
    scale_up_max: float = Field(default=1.0, ge=1.0, le=3.0)  # >1: trade bigger once ahead, up to this multiple
    margin_use: float = Field(gt=0, le=1)


class PropConfig(_Strict):
    enabled: bool                                      # true = the bot trades the prop account(s), not CoinSpot
    broker: Literal["paper", "dxtrade"]
    firm: Literal["tradeify_2step", "tradeify_1step"]
    account_size: float = Field(gt=0)
    phase: Literal["1", "2", "funded"]
    paper_accounts: int = Field(ge=1, le=5)
    symbols: dict[str, str] = Field(min_length=1)     # coin -> the firm's symbol, e.g. BTC: BTC/USD
    price_symbols: dict[str, str] = Field(min_length=1)  # coin -> Binance symbol (prices for decisions)
    quantity_step: dict[str, float] = Field(default_factory=dict)  # coin -> order size step (default 0.0001)
    news_aliases: dict[str, list[str]] = Field(default_factory=dict)
    strategy: PropStrategyConfig
    risk: PropRiskConfig
    ai_review: Literal["off", "shadow", "veto"]
    payout_alert_usd: float = Field(ge=100)

    @model_validator(mode="after")
    def _check(self) -> PropConfig:
        missing = sorted(set(self.symbols) - set(self.price_symbols))
        if missing:
            raise ValueError(f"prop.price_symbols needs an entry for {missing}")
        if self.strategy.regime_coin not in self.symbols:
            raise ValueError(f"prop.strategy.regime_coin '{self.strategy.regime_coin}' must be in prop.symbols")
        if self.risk.daily_brake_pct >= 3.0:
            raise ValueError("prop.risk.daily_brake_pct must be below the firm's 3% daily loss limit")
        return self


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    file_max_mb: float = Field(gt=0, le=1000)
    file_backups: int = Field(ge=1, le=100)


class PathsConfig(_Strict):
    database: str
    logs: str
    reports: str


class Config(_Strict):
    timezone: str
    coins: list[CoinConfig] = Field(min_length=1)
    schedule: ScheduleConfig
    strategy: StrategyConfig
    execution: ExecutionConfig
    data: DataConfig
    scanner: ScannerConfig
    news: NewsConfig
    risk: RiskConfig
    fees: FeesConfig
    exits: ExitsConfig
    paper: PaperConfig
    backtest: BacktestConfig
    llm: LLMConfig
    live: LiveConfig
    watchdog: WatchdogConfig
    prop: PropConfig
    logging: LoggingConfig
    paths: PathsConfig

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone '{v}' (example: Australia/Sydney)") from exc
        return v

    @model_validator(mode="after")
    def _unique_coins(self) -> Config:
        codes = [c.code for c in self.coins]
        dupes = sorted({c for c in codes if codes.count(c) > 1})
        if dupes:
            raise ValueError(f"coin(s) listed twice: {dupes}")
        s = self.scanner
        for role in ("trend_timeframe", "setup_timeframe", "trigger_timeframe"):
            tf = getattr(s, role)
            if tf not in self.data.timeframes:
                raise ValueError(f"scanner.{role} '{tf}' must also be listed in data.timeframes")
        needed = max(s.ema_trend, s.macd_slow + s.macd_signal, s.volume_lookback, s.breakout_lookback) + 20
        if self.data.candles_per_timeframe < needed:
            raise ValueError(f"data.candles_per_timeframe must be at least {needed} for these scanner settings")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def coin(self, code: str) -> CoinConfig:
        code = code.upper()
        for c in self.coins:
            if c.code == code:
                return c
        raise KeyError(f"{code} is not in the coins list in config.yaml")

    def path(self, name: Literal["database", "logs", "reports", "candle_cache"]) -> Path:
        """Absolute path for one of the `paths:` entries (or backtest.candle_cache)."""
        p = Path(self.backtest.candle_cache if name == "candle_cache" else getattr(self.paths, name))
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} is empty or not a settings file")
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        lines = [f"{path.name} has invalid settings:"]
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"])
            lines.append(f"  - {where}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from None


# ---------------------------------------------------------------------------
# Secrets (.env)
# ---------------------------------------------------------------------------

SECRET_ENV_NAMES = (
    "COINSPOT_READONLY_KEY",
    "COINSPOT_READONLY_SECRET",
    "COINSPOT_KEY",
    "COINSPOT_SECRET",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "CRYPTOPANIC_TOKEN",
    "HEALTHCHECKS_URL",
    "DASHBOARD_PASSWORD",
    "DXTRADE_USERNAME",
    "DXTRADE_PASSWORD",
)


@dataclass(frozen=True, repr=False)
class Secrets:
    """Keys from .env. repr() never shows values, so they can't leak into logs."""

    live_trading: bool = False
    coinspot_readonly_key: str | None = None
    coinspot_readonly_secret: str | None = None
    coinspot_key: str | None = None
    coinspot_secret: str | None = None
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    cryptopanic_token: str | None = None
    healthchecks_url: str | None = None
    dashboard_password: str | None = None
    dxtrade_url: str | None = None          # e.g. https://<server>/dxsca-web (from the firm)
    dxtrade_username: str | None = None
    dxtrade_password: str | None = None
    dxtrade_domain: str | None = None       # usually "default"
    dxtrade_accounts: str | None = None     # comma-separated account codes; blank = all of them
    problems: tuple[str, ...] = field(default=())

    def __repr__(self) -> str:
        present = [n for n in self.__dataclass_fields__ if n not in ("live_trading", "problems") and getattr(self, n)]
        return f"Secrets(live_trading={self.live_trading}, present={present})"

    __str__ = __repr__

    @property
    def has_readonly_key(self) -> bool:
        return bool(self.coinspot_readonly_key and self.coinspot_readonly_secret)

    @property
    def has_full_key(self) -> bool:
        return bool(self.coinspot_key and self.coinspot_secret)

    def secret_values(self) -> list[str]:
        """Every secret value, for the log redaction filter."""
        public = ("live_trading", "problems", "telegram_chat_id", "dxtrade_url", "dxtrade_domain", "dxtrade_accounts")
        names = [n for n in self.__dataclass_fields__ if n not in public]
        return [v for v in (getattr(self, n) for n in names) if v]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip('"').strip("'").strip()
    return value or None


def load_secrets(env_path: str | os.PathLike | None = None, environ: dict[str, str] | None = None) -> Secrets:
    """Read secrets from .env, letting real environment variables (e.g. Docker) win."""
    env_path = Path(env_path) if env_path else DEFAULT_ENV_PATH
    values: dict[str, str | None] = {}
    if env_path.exists():
        values.update(dotenv_values(env_path))
    values.update(os.environ if environ is None else environ)

    def get(name: str) -> str | None:
        return _clean(values.get(name))

    problems: list[str] = []
    live_raw = (get("LIVE_TRADING") or "false").lower()
    if live_raw not in ("true", "false"):
        problems.append(f"LIVE_TRADING must be true or false (got '{live_raw}'); treating it as false")
    live = live_raw == "true"

    pairs = [
        ("COINSPOT_READONLY_KEY", "COINSPOT_READONLY_SECRET"),
        ("COINSPOT_KEY", "COINSPOT_SECRET"),
    ]
    for a, b in pairs:
        if bool(get(a)) != bool(get(b)):
            problems.append(f"{a} and {b} must both be filled in (one is blank)")

    return Secrets(
        live_trading=live,
        coinspot_readonly_key=get("COINSPOT_READONLY_KEY"),
        coinspot_readonly_secret=get("COINSPOT_READONLY_SECRET"),
        coinspot_key=get("COINSPOT_KEY"),
        coinspot_secret=get("COINSPOT_SECRET"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=get("TELEGRAM_CHAT_ID"),
        cryptopanic_token=get("CRYPTOPANIC_TOKEN"),
        healthchecks_url=get("HEALTHCHECKS_URL"),
        dashboard_password=get("DASHBOARD_PASSWORD"),
        dxtrade_url=get("DXTRADE_URL"),
        dxtrade_username=get("DXTRADE_USERNAME"),
        dxtrade_password=get("DXTRADE_PASSWORD"),
        dxtrade_domain=get("DXTRADE_DOMAIN"),
        dxtrade_accounts=get("DXTRADE_ACCOUNTS"),
        problems=tuple(problems),
    )
