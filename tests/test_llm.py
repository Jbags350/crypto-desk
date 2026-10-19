from datetime import timedelta

import anthropic
import httpx2
import pytest

from desk.clock import iso
from desk.llm import LLMBadOutput, LLMBudgetExceeded, LLMClient, LLMUnavailable
from desk.schemas import NewsAssessment
from tests.fakes import FakeAnthropic, fake_response

GOOD = {"coins": [{"coin": "btc", "sentiment": 0.3, "reason": "ETF inflows\n strong", "major_event": False,
                   "event_summary": ""}]}


def make(config, db, clock, *responses, **cfg_overrides):
    cfg = config.llm.model_copy(update=cfg_overrides) if cfg_overrides else config.llm
    fake = FakeAnthropic(*responses)
    return LLMClient(cfg, db, clock, client=fake), fake


def test_successful_call_parses_records_and_costs(config, db, clock):
    llm, fake = make(config, db, clock, fake_response(GOOD, input_tokens=2000, output_tokens=400))
    res = llm.call("news_analyst", "system prompt " * 10, "user msg", NewsAssessment)
    c = res.output.coins[0]
    assert c.coin == "BTC" and c.sentiment == 0.3 and c.reason == "ETF inflows strong"
    assert res.cost_usd == pytest.approx(2000 * 0.10 / 1e6 + 400 * 0.50 / 1e6)
    row = db.query_one("SELECT * FROM llm_calls")
    assert row["ok"] == 1 and row["agent"] == "news_analyst" and row["model"] == "claude-haiku-5-5"
    assert row["cost_usd"] == pytest.approx(res.cost_usd)
    sent = fake.calls[0]
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in sent      # Haiku 5.5 rejects non-default sampling params


def test_thinking_block_before_text_is_fine(config, db, clock):
    llm, _ = make(config, db, clock, fake_response(GOOD, thinking=True))
    assert llm.call("a", "s" * 60, "u", NewsAssessment).output.coins[0].coin == "BTC"


def test_sentiment_clamped(config, db, clock):
    bad = {"coins": [{**GOOD["coins"][0], "sentiment": 3.5}]}
    llm, _ = make(config, db, clock, fake_response(bad))
    assert llm.call("a", "s" * 60, "u", NewsAssessment).output.coins[0].sentiment == 1.0


def test_wrong_shape_is_bad_output_but_cost_still_recorded(config, db, clock):
    llm, _ = make(config, db, clock, fake_response({"nope": 1}, input_tokens=500, output_tokens=100))
    with pytest.raises(LLMBadOutput) as ei:
        llm.call("a", "s" * 60, "u", NewsAssessment)
    row = db.query_one("SELECT * FROM llm_calls WHERE id = ?", (ei.value.call_id,))
    assert row["ok"] == 0 and row["cost_usd"] > 0 and "format" in row["error"]


@pytest.mark.parametrize("stop,msg", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_refusal_and_truncation(config, db, clock, stop, msg):
    llm, _ = make(config, db, clock, fake_response(GOOD, stop_reason=stop))
    with pytest.raises(LLMBadOutput, match=msg):
        llm.call("a", "s" * 60, "u", NewsAssessment)


def test_no_key_means_unavailable(config, db, clock):
    llm = LLMClient(config.llm, db, clock, api_key=None)
    assert not llm.available
    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        llm.call("a", "s", "u", NewsAssessment)


def test_connection_error_mapped_and_recorded(config, db, clock):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    llm, _ = make(config, db, clock, anthropic.APIConnectionError(request=req))
    with pytest.raises(LLMUnavailable, match="Couldn't reach"):
        llm.call("a", "s" * 60, "u", NewsAssessment)
    row = db.query_one("SELECT * FROM llm_calls")
    assert row["ok"] == 0 and row["cost_usd"] == 0


def test_bad_key_mapped(config, db, clock):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req), body=None)
    llm, _ = make(config, db, clock, err)
    with pytest.raises(LLMUnavailable, match="rejected the API key"):
        llm.call("a", "s" * 60, "u", NewsAssessment)


def _spend(db, clock, usd, when=None):
    db.execute("INSERT INTO llm_calls(ts, agent, model, input_tokens, output_tokens, cache_write_tokens, "
               "cache_read_tokens, cost_usd, latency_ms, ok) VALUES (?, 'x', 'm', 0, 0, 0, 0, ?, 0, 1)",
               (iso(when or clock.now()), usd))


def test_budget_cap_blocks_call_before_it_is_made(config, db, clock):
    llm, fake = make(config, db, clock, fake_response(GOOD), daily_spend_cap_usd=0.05)
    _spend(db, clock, 0.0495)
    with pytest.raises(LLMBudgetExceeded, match="Daily AI budget"):
        llm.call("a", "s" * 60, "u", NewsAssessment)
    assert fake.calls == []


def test_budget_resets_at_local_midnight(config, db, clock):
    llm, fake = make(config, db, clock, fake_response(GOOD), daily_spend_cap_usd=0.05)
    _spend(db, clock, 0.05)
    assert llm.spent_today_usd() == pytest.approx(0.05)
    clock.set(clock.next_day_start_utc() + timedelta(minutes=1))
    assert llm.spent_today_usd() == 0
    llm.call("a", "s" * 60, "u", NewsAssessment)
    assert len(fake.calls) == 1


def test_zero_cap_disables_ai(config, db, clock):
    llm, fake = make(config, db, clock, fake_response(GOOD), daily_spend_cap_usd=0)
    with pytest.raises(LLMBudgetExceeded):
        llm.call("a", "s", "u", NewsAssessment)


def test_cache_tokens_priced(config, db, clock):
    llm, _ = make(config, db, clock)
    assert llm.cost_usd(0, 0, cache_write=1_000_000) == pytest.approx(0.10 * 1.25)
    assert llm.cost_usd(0, 0, cache_read=1_000_000) == pytest.approx(0.10 * 0.10)
