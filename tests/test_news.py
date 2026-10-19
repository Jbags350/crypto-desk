from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from desk.news import ALL_COINS, CoinTagger, EventDetector, NewsFeed, NewsItem, clean_text, parse_cryptopanic, parse_rss

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Ethereum ETF sees record inflows</title><link>https://ex.com/a</link>
  <description>&lt;p&gt;Big &lt;b&gt;day&lt;/b&gt; for ETH&lt;/p&gt;</description>
  <pubDate>Fri, 09 Oct 2026 07:30:00 GMT</pubDate></item>
<item><title>CoinSpot suspends withdrawals during outage</title><link>https://ex.com/b</link>
  <pubDate>Fri, 09 Oct 2026 07:45:00 GMT</pubDate></item>
<item><title>Old news: Bitcoin hits a record</title><link>https://ex.com/c</link>
  <pubDate>Mon, 05 Oct 2026 07:45:00 GMT</pubDate></item>
</channel></rss>"""

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)


def test_clean_text_strips_html():
    assert clean_text("<p>Big <b>day</b> &amp; night</p>") == "Big day & night"


def test_parse_rss():
    items = parse_rss(RSS.encode(), "ex.com", NOW)
    assert [i.title for i in items][0] == "Ethereum ETF sees record inflows"
    assert items[0].summary == "Big day for ETH"
    assert items[0].published_at == datetime(2026, 10, 9, 7, 30, tzinfo=UTC)
    assert len({i.id for i in items}) == 3


@pytest.mark.parametrize("text,expected", [
    ("Ethereum ETF sees inflows", ["ETH"]),
    ("Tether mints more USDT", []),                    # 'ether' inside 'Tether' is not ETH
    ("A new solution for payments", []),               # 'sol' inside 'solution' is not SOL
    ("$SOL rallies 10%", ["SOL"]),
    ("BTC and ETH slide as Solana holds", ["BTC", "ETH", "SOL"]),
    ("bitcoin miners sell", ["BTC"]),
])
def test_coin_tagging(config, text, expected):
    assert CoinTagger(config.coins).tag(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Exchange hacked for $40M", ["hack"]),
    ("SEC sues crypto firm", ["regulation"]),
    ("Network halted after bug", ["outage"]),
    ("Stablecoin depegged", ["insolvency"]),
    ("Price rises on strong demand", []),
    ("Second-largest exchange", []),                    # 'sec' must be a whole word
])
def test_event_detection(config, text, expected):
    assert EventDetector(config.news.event_keywords).kinds(text) == expected


def make_feed(config, db, clock, feeds, token=None):
    cfg = config.model_copy(update={"news": config.news.model_copy(update={"rss_feeds": feeds})})
    return NewsFeed(cfg, httpx.Client(), clock, db, cryptopanic_token=token)


@respx.mock
def test_fetch_all_stores_dedupes_and_blocks_on_exchange_outage(config, db, clock):
    respx.get("https://feed.one/rss").mock(return_value=httpx.Response(200, text=RSS))
    respx.get("https://feed.two/rss").mock(return_value=httpx.Response(500))
    nf = make_feed(config, db, clock, ["https://feed.one/rss", "https://feed.two/rss"])
    rep = nf.fetch_all()
    assert rep.fetched == 3 and rep.new == 3
    assert rep.per_source == {"feed.one": 3}
    assert any("feed.two" in e for e in rep.errors)
    assert len(rep.exchange_blocks) == 1
    blocks = nf.active_blocks("BTC")
    assert blocks and blocks[0]["coin"] == ALL_COINS and blocks[0]["kind"] == "exchange_outage"

    rep2 = nf.fetch_all()                       # same feed again: nothing new, no second block
    assert rep2.new == 0 and rep2.exchange_blocks == []
    assert len(nf.active_blocks()) == 1


@respx.mock
def test_old_exchange_news_does_not_block(config, db, clock):
    old = RSS.replace("Fri, 09 Oct 2026 07:45:00 GMT", "Mon, 05 Oct 2026 07:45:00 GMT")
    respx.get("https://feed.one/rss").mock(return_value=httpx.Response(200, text=old))
    nf = make_feed(config, db, clock, ["https://feed.one/rss"])
    assert nf.fetch_all().exchange_blocks == []


def test_recent_filters_by_coin_market_and_age(config, db, clock):
    nf = make_feed(config, db, clock, [])
    def add(title, coins, minutes_ago):
        nf._store(NewsItem(id=title, source="s", title=title, url=None, summary="",
                           published_at=clock.now() - timedelta(minutes=minutes_ago), coins=coins))
    add("btc new", ["BTC"], 5)
    add("btc old", ["BTC"], 60 * 20)            # older than max_age_hours (12)
    add("both", ["BTC", "ETH"], 10)
    add("market", [], 15)
    assert [i.title for i in nf.recent("BTC")] == ["btc new", "both"]
    assert [i.title for i in nf.recent("ETH")] == ["both"]
    assert [i.title for i in nf.recent(market_only=True)] == ["market"]
    assert len(nf.recent()) == 3


def test_blocks_expire(config, db, clock):
    nf = make_feed(config, db, clock, [])
    nf.add_block("SOL", "major_event", "test", "unit", hours=2)
    assert nf.active_blocks("SOL") and not nf.active_blocks("BTC")
    clock.advance(hours=2, seconds=1)
    assert nf.active_blocks("SOL") == []


def test_parse_cryptopanic_both_formats():
    data = {"results": [
        {"title": "BTC up", "published_at": "2026-10-09T07:00:00Z", "url": "https://cp/1",
         "source": {"domain": "coindesk.com"}},
        {"title": "ETH down", "published_at": "bad-date"},
        {"title": ""},
    ]}
    items = parse_cryptopanic(data, NOW)
    assert [i.title for i in items] == ["BTC up", "ETH down"]
    assert items[0].source == "cryptopanic:coindesk.com"
    assert items[1].published_at == NOW


@respx.mock
def test_cryptopanic_used_with_token_and_failure_tolerated(config, db, clock):
    route = respx.get(config.news.cryptopanic_url).mock(
        return_value=httpx.Response(401, json={"status": "api_error", "info": "Token not found"}))
    nf = make_feed(config, db, clock, [], token="tok123")
    rep = nf.fetch_all()
    assert route.called
    assert route.calls.last.request.url.params["auth_token"] == "tok123"
    assert any("cryptopanic" in e and "Token not found" in e for e in rep.errors)


@respx.mock
def test_no_cryptopanic_call_without_token(config, db, clock):
    route = respx.get(config.news.cryptopanic_url).mock(return_value=httpx.Response(200, json={"results": []}))
    make_feed(config, db, clock, []).fetch_all()
    assert not route.called
