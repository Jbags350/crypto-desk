"""News fetching, coin tagging and event detection (no AI here).

- Pulls headlines from the RSS feeds in config.yaml, plus CryptoPanic if a
  token is set in .env.
- Tags each headline with the coins it mentions (whole words only, so
  "Tether" is not ETH and "solution" is not SOL).
- Marks headlines containing event words (hack, outage, SEC...) as
  "possible events". The News Analyst AI decides if they are really major.
- Exception: hack/outage news that names our exchange (CoinSpot) blocks ALL
  new trades immediately, without waiting for the AI.
"""

from __future__ import annotations

import calendar
import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import feedparser
import httpx

from desk.clock import Clock, iso, parse_iso
from desk.config import Config
from desk.db import Database

log = logging.getLogger("desk.news")

ALL_COINS = "*"                     # news_events.coin value meaning "every coin"
EXCHANGE_BLOCK_KINDS = ("hack", "outage", "insolvency")


@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    url: str | None
    summary: str
    published_at: datetime
    coins: list[str] = field(default_factory=list)
    event_kinds: list[str] = field(default_factory=list)


def item_id(url: str | None, title: str) -> str:
    key = (url or "").strip() or title.strip().lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


_TAGS = re.compile(r"<[^>]+>")


def clean_text(text: str | None, limit: int = 400) -> str:
    text = html.unescape(_TAGS.sub(" ", text or ""))
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _words_regex(words: list[str]) -> re.Pattern | None:
    words = [w.strip() for w in words if w and w.strip()]
    if not words:
        return None
    parts = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])", re.IGNORECASE)


class CoinTagger:
    def __init__(self, coins):
        self._rx = {c.code: _words_regex([c.code, *c.news_aliases]) for c in coins}

    def tag(self, text: str) -> list[str]:
        return [code for code, rx in self._rx.items() if rx is not None and rx.search(text)]


class EventDetector:
    def __init__(self, event_keywords: dict[str, list[str]]):
        self._rx = {kind: _words_regex(words) for kind, words in event_keywords.items()}

    def kinds(self, text: str) -> list[str]:
        return [kind for kind, rx in self._rx.items() if rx is not None and rx.search(text)]


def _when(entry, now: datetime) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            dt = datetime.fromtimestamp(calendar.timegm(t), UTC)
            return min(dt, now)         # some feeds post future-dated items
    return now


def parse_rss(content: bytes, source: str, now: datetime) -> list[NewsItem]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        title = clean_text(e.get("title"), 300)
        if not title:
            continue
        url = e.get("link")
        items.append(NewsItem(id=item_id(url, title), source=source, title=title, url=url,
                              summary=clean_text(e.get("summary") or e.get("description")),
                              published_at=_when(e, now)))
    return items


def parse_cryptopanic(data: dict, now: datetime) -> list[NewsItem]:
    items = []
    for p in (data or {}).get("results") or []:
        title = clean_text(p.get("title"), 300)
        if not title:
            continue
        url = p.get("original_url") or p.get("url")
        try:
            published = min(parse_iso(p["published_at"]), now) if p.get("published_at") else now
        except ValueError:
            published = now
        src = p.get("source")
        source = src.get("domain") or src.get("title") if isinstance(src, dict) else "cryptopanic"
        items.append(NewsItem(id=item_id(url, title), source=f"cryptopanic:{source or 'unknown'}", title=title,
                              url=url, summary=clean_text(p.get("description")), published_at=published))
    return items


@dataclass
class FetchReport:
    fetched: int = 0
    new: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    exchange_blocks: list[str] = field(default_factory=list)


class NewsFeed:
    def __init__(self, config: Config, http: httpx.Client, clock: Clock, db: Database,
                 cryptopanic_token: str | None = None):
        self.config = config
        self.http = http
        self.clock = clock
        self.db = db
        self.cryptopanic_token = cryptopanic_token
        self.tagger = CoinTagger(config.coins)
        self.events = EventDetector(config.news.event_keywords)
        self._exchange_rx = _words_regex(config.news.exchange_names)

    # ---- fetching --------------------------------------------------------
    def fetch_all(self) -> FetchReport:
        report = FetchReport()
        now = self.clock.now()
        batches: list[tuple[str, list[NewsItem]]] = []
        for url in self.config.news.rss_feeds:
            source = (urlparse(url).hostname or url).removeprefix("www.")
            try:
                r = self.http.get(url, timeout=20, follow_redirects=True)
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code}")
                batches.append((source, parse_rss(r.content, source, now)))
            except Exception as exc:   # one broken feed must not stop the others
                report.errors.append(f"{source}: {type(exc).__name__} {exc}".strip())
        if self.cryptopanic_token:
            try:
                codes = ",".join(c.code for c in self.config.coins)
                r = self.http.get(self.config.news.cryptopanic_url, timeout=20,
                                  params={"auth_token": self.cryptopanic_token, "currencies": codes, "public": "true"})
                data = r.json()
                if r.status_code != 200 or not isinstance(data, dict) or "results" not in data:
                    raise ValueError(f"HTTP {r.status_code}: {str(data.get('info') if isinstance(data, dict) else data)[:120]}")
                batches.append(("cryptopanic", parse_cryptopanic(data, now)))
            except Exception as exc:
                report.errors.append(f"cryptopanic: {type(exc).__name__} {exc}".strip())

        cutoff = now - timedelta(hours=self.config.news.max_age_hours)
        for source, items in batches:
            report.per_source[source] = len(items)
            for it in items:
                report.fetched += 1
                text = f"{it.title} {it.summary}"
                it.coins = self.tagger.tag(text)
                it.event_kinds = self.events.kinds(text)
                if self._store(it):
                    report.new += 1
                    if it.published_at >= cutoff:
                        block = self._exchange_block(it)
                        if block:
                            report.exchange_blocks.append(block)
        for err in report.errors:
            log.warning("News feed problem: %s", err)
        return report

    def _store(self, it: NewsItem) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO news_items(id, source, title, url, summary, published_at, fetched_at, coins, event_kinds) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (it.id, it.source, it.title, it.url, it.summary, iso(it.published_at), iso(self.clock.now()),
             json.dumps(it.coins), json.dumps(it.event_kinds)),
        )
        return cur.rowcount == 1

    def _exchange_block(self, it: NewsItem) -> str | None:
        """Hack/outage news about our own exchange blocks every coin straight away."""
        if self._exchange_rx is None or not self._exchange_rx.search(f"{it.title} {it.summary}"):
            return None
        kinds = [k for k in it.event_kinds if k in EXCHANGE_BLOCK_KINDS]
        if not kinds:
            return None
        summary = f"Exchange {kinds[0]} reported: {it.title}"
        self.add_block(ALL_COINS, f"exchange_{kinds[0]}", summary, it.source)
        log.warning("Blocking ALL new trades: %s", summary)
        return summary

    # ---- reading -----------------------------------------------------------
    def recent(self, coin: str | None = None, *, market_only: bool = False, limit: int | None = None,
               hours: float | None = None) -> list[NewsItem]:
        since = self.clock.now() - timedelta(hours=hours or self.config.news.max_age_hours)
        sql = "SELECT * FROM news_items WHERE published_at >= ?"
        params: list = [iso(since)]
        if coin:
            sql += " AND coins LIKE ?"
            params.append(f'%"{coin.upper()}"%')
        elif market_only:
            sql += " AND coins = '[]'"
        sql += " ORDER BY published_at DESC, id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [NewsItem(id=r["id"], source=r["source"], title=r["title"], url=r["url"], summary=r["summary"] or "",
                         published_at=parse_iso(r["published_at"]), coins=json.loads(r["coins"]),
                         event_kinds=json.loads(r["event_kinds"]))
                for r in self.db.query(sql, params)]

    # ---- trading blocks from major events ------------------------------------
    def add_block(self, coin: str, kind: str, summary: str, source: str, hours: float | None = None) -> None:
        hours = self.config.risk.major_event_block_hours if hours is None else hours
        now = self.clock.now()
        self.db.execute(
            "INSERT INTO news_events(ts, coin, kind, summary, source, block_until) VALUES (?,?,?,?,?,?)",
            (iso(now), coin.upper() if coin != ALL_COINS else ALL_COINS, kind, summary, source,
             iso(now + timedelta(hours=hours))),
        )

    def active_blocks(self, coin: str | None = None) -> list[dict]:
        """Major events still blocking new trades (for `coin`, including all-coin blocks)."""
        sql = "SELECT * FROM news_events WHERE block_until > ?"
        params: list = [iso(self.clock.now())]
        if coin:
            sql += " AND (coin = ? OR coin = ?)"
            params += [coin.upper(), ALL_COINS]
        sql += " ORDER BY ts DESC"
        return [dict(r) for r in self.db.query(sql, params)]
