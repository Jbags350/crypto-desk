from pathlib import Path

import pytest
import yaml

from desk.config import DEFAULT_CONFIG_PATH, ConfigError, load_config, load_secrets


def _write_variant(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_real_config_is_valid():
    config = load_config(DEFAULT_CONFIG_PATH)          # exactly as written in config.yaml
    assert [c.code for c in config.coins][:3] == ["BTC", "ETH", "SOL"]
    assert config.strategy.mode == "trend" and config.risk.max_position_pct == 10.0
    assert config.strategy.trend.stop_atr_multiple == 2.0
    assert config.risk.max_open_positions == 3
    assert config.risk.daily_loss_limit_pct == 3.0
    assert config.risk.max_entries_per_day == 6
    assert config.llm.model == "claude-haiku-5-5"
    assert config.fees.market_order_pct == 0.1


def test_coin_lookup_and_coinspot_code(config):
    assert config.coin("btc").coinspot == "btc"
    with pytest.raises(KeyError):
        config.coin("NOPE")


def test_misspelled_setting_is_rejected(tmp_path):
    p = _write_variant(tmp_path, lambda r: r["risk"].__setitem__("max_postion_pct", 5))
    with pytest.raises(ConfigError, match="max_postion_pct"):
        load_config(p)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("risk", "max_position_pct", -1),
        ("risk", "max_position_pct", 0),
        ("risk", "max_open_positions", 0),
        ("risk", "daily_loss_limit_pct", 0),
        ("risk", "min_confidence", 101),
        ("schedule", "daily_summary_time", "25:00"),
        ("schedule", "fast_check_seconds", 1),
        ("llm", "effort", "turbo"),
        ("data", "timeframes", ["7m"]),
        ("data", "candle_providers", ["coinbase"]),
    ],
)
def test_bad_values_are_rejected(tmp_path, section, key, value):
    p = _write_variant(tmp_path, lambda r: r[section].__setitem__(key, value))
    with pytest.raises(ConfigError):
        load_config(p)


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r["scanner"].update(ema_mid=60), "EMAs must be in order"),
    (lambda r: r["scanner"].update(macd_fast=30), "macd_fast"),
    (lambda r: r["scanner"].update(pullback_rsi=20), "RSI levels"),
    (lambda r: r["scanner"].update(trend_timeframe="4h"), "data.timeframes"),
    (lambda r: r["data"].update(candles_per_timeframe=100), "candles_per_timeframe"),
    (lambda r: r["scanner"].update(setups=["moon_shot"]), "setups"),
    (lambda r: r["news"].update(rss_feeds=["ftp://x"]), "https"),
])
def test_scanner_and_news_rules(tmp_path, mutate, msg):
    p = _write_variant(tmp_path, mutate)
    with pytest.raises(ConfigError, match=msg):
        load_config(p)


def test_bad_timezone_rejected(tmp_path):
    p = _write_variant(tmp_path, lambda r: r.__setitem__("timezone", "Mars/Olympus"))
    with pytest.raises(ConfigError, match="time zone"):
        load_config(p)


def test_duplicate_coin_rejected(tmp_path):
    p = _write_variant(tmp_path, lambda r: r["coins"].append(dict(r["coins"][0])))
    with pytest.raises(ConfigError, match="twice"):
        load_config(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_paths_are_absolute(config):
    assert config.path("database").is_absolute()


# ---- secrets ---------------------------------------------------------

def test_paper_is_default(tmp_path):
    s = load_secrets(tmp_path / "missing.env", environ={})
    assert s.live_trading is False
    assert not s.has_readonly_key and not s.has_full_key


@pytest.mark.parametrize("raw,expected", [("true", True), ("TRUE", True), ("false", False),
                                          ("yes", False), ("1", False), ("", False)])
def test_live_flag_only_exact_true(tmp_path, raw, expected):
    s = load_secrets(tmp_path / "x.env", environ={"LIVE_TRADING": raw})
    assert s.live_trading is expected


def test_env_file_read_and_quotes_stripped(tmp_path):
    env = tmp_path / ".env"
    env.write_text('COINSPOT_READONLY_KEY="abc123"\nCOINSPOT_READONLY_SECRET=\'s3cr3t\'\n', encoding="utf-8")
    s = load_secrets(env, environ={})
    assert s.coinspot_readonly_key == "abc123"
    assert s.coinspot_readonly_secret == "s3cr3t"
    assert s.has_readonly_key


def test_half_filled_key_pair_reported(tmp_path):
    s = load_secrets(tmp_path / "x.env", environ={"COINSPOT_KEY": "abc"})
    assert any("COINSPOT_SECRET" in p for p in s.problems)
    assert not s.has_full_key


def test_secrets_never_shown_in_repr(tmp_path):
    s = load_secrets(tmp_path / "x.env", environ={"ANTHROPIC_API_KEY": "sk-ant-supersecretvalue"})
    assert "supersecret" not in repr(s)
    assert "supersecret" not in str(s)
    assert "sk-ant-supersecretvalue" in s.secret_values()
