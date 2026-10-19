"""Builds the shared pieces every entry point needs (config, logs, DB, API clients)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from desk import __version__
from desk.clock import Clock
from desk.config import Config, Secrets, load_config, load_secrets
from desk.data.candles import CandleFeed, build_providers
from desk.data.market import MarketData
from desk.db import Database
from desk.exchange.coinspot import CoinSpotClient, NonceSource
from desk.llm import LLMClient
from desk.log_setup import setup_logging
from desk.news import NewsFeed
from desk.scanner import Scanner
from desk.strategy import apply_strategy

USER_AGENT = f"crypto-desk/{__version__}"


@dataclass
class App:
    config: Config
    secrets: Secrets
    clock: Clock
    db: Database
    coinspot: CoinSpotClient
    candles: CandleFeed
    market: MarketData
    log: logging.Logger
    http: httpx.Client          # shared client for candles/news/etc. (CoinSpot has its own)
    news: NewsFeed
    llm: LLMClient
    scanner: Scanner

    def close(self) -> None:
        self.coinspot.close()
        self.http.close()
        self.db.close()


def build_app(process_name: str = "desk", *, config_path: str | Path | None = None,
              env_path: str | Path | None = None, console_logging: bool = True) -> App:
    config = apply_strategy(load_config(config_path))     # the settings the chosen strategy mode really uses
    secrets = load_secrets(env_path)
    log = setup_logging(
        config.path("logs"),
        process_name=process_name,
        level=config.logging.level,
        max_mb=config.logging.file_max_mb,
        backups=config.logging.file_backups,
        secrets=secrets.secret_values(),
        console=console_logging,
    )
    for problem in secrets.problems:
        log.warning(".env: %s", problem)

    clock = Clock(config.tz)
    db = Database(config.path("database"), clock)
    db.migrate()

    def _load_nonce() -> int | None:
        v = db.kv_get("coinspot_nonce")
        return int(v) if v and v.isdigit() else None

    nonce = NonceSource(load=_load_nonce, save=lambda n: db.kv_set("coinspot_nonce", str(n)))
    coinspot = CoinSpotClient(
        readonly_key=secrets.coinspot_readonly_key,
        readonly_secret=secrets.coinspot_readonly_secret,
        key=secrets.coinspot_key,
        secret=secrets.coinspot_secret,
        http=httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT}),
        nonce=nonce,
        clock=clock,
    )
    http = httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    candles = CandleFeed(build_providers(config.data.candle_providers, http, clock), clock)
    market = MarketData(coinspot, config.coins, clock)
    news = NewsFeed(config, http, clock, db, cryptopanic_token=secrets.cryptopanic_token)
    llm = LLMClient(config.llm, db, clock, api_key=secrets.anthropic_api_key)
    scanner = Scanner(config, candles, clock, db)
    return App(config=config, secrets=secrets, clock=clock, db=db, coinspot=coinspot,
               candles=candles, market=market, log=log, http=http, news=news, llm=llm, scanner=scanner)
