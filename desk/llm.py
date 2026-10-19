"""Claude API wrapper shared by every AI agent.

- Structured output: the answer must match a pydantic schema, so agents get
  validated data, never free text to guess at.
- Daily spend cap: before each call, today's spend plus this call's worst case
  is checked against llm.daily_spend_cap_usd. Over the cap -> no call.
- Every call (good or bad) is recorded in the llm_calls table with tokens and
  cost, so the cap and the dashboard always know what was spent.
- Any failure raises an LLMError. Callers treat that as "no AI opinion",
  which means no new trade. Code-based stops never depend on the AI.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from desk.clock import Clock, iso
from desk.config import LLMConfig
from desk.db import Database

log = logging.getLogger("desk.llm")

T = TypeVar("T", bound=BaseModel)

CACHE_WRITE_MULTIPLIER = 1.25   # prompt-cache writes cost 1.25x normal input
CACHE_READ_MULTIPLIER = 0.10    # prompt-cache reads cost 0.1x normal input
CHARS_PER_TOKEN_ESTIMATE = 3.0  # cautious (over-)estimate for the budget check


class LLMError(Exception):
    """The AI couldn't give a usable answer."""

    def __init__(self, message: str, call_id: int | None = None):
        super().__init__(message)
        self.call_id = call_id


class LLMUnavailable(LLMError):
    """No API key, network problem, or the Anthropic API is down."""


class LLMBudgetExceeded(LLMError):
    """Today's AI spending cap has been reached."""


class LLMBadOutput(LLMError):
    """The AI answered, but the answer wasn't usable (refused, cut off, wrong shape)."""


@dataclass
class LLMResult(Generic[T]):
    output: T
    call_id: int
    cost_usd: float
    input_tokens: int
    output_tokens: int


class LLMClient:
    def __init__(self, cfg: LLMConfig, db: Database, clock: Clock, api_key: str | None = None, client=None):
        self.cfg = cfg
        self.db = db
        self.clock = clock
        if client is None and api_key:
            client = anthropic.Anthropic(api_key=api_key, timeout=cfg.timeout_seconds, max_retries=2)
        self._client = client
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._client is not None

    # ---- money ---------------------------------------------------------
    def cost_usd(self, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
        p_in = self.cfg.price_input_per_mtok_usd / 1_000_000
        p_out = self.cfg.price_output_per_mtok_usd / 1_000_000
        return (input_tokens * p_in + output_tokens * p_out
                + cache_write * p_in * CACHE_WRITE_MULTIPLIER + cache_read * p_in * CACHE_READ_MULTIPLIER)

    def spent_today_usd(self) -> float:
        row = self.db.query_one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_calls WHERE ts >= ?",
                                (iso(self.clock.day_start_utc()),))
        return float(row["s"])

    def worst_case_cost(self, system: str, user: str) -> float:
        est_in = int((len(system) + len(user)) / CHARS_PER_TOKEN_ESTIMATE) + 200
        return self.cost_usd(est_in, self.cfg.max_tokens)

    def budget_left_usd(self) -> float:
        return max(0.0, self.cfg.daily_spend_cap_usd - self.spent_today_usd())

    # ---- the call --------------------------------------------------------
    def call(self, agent: str, system: str, user: str, output_model: type[T]) -> LLMResult[T]:
        if self._client is None:
            raise LLMUnavailable("No ANTHROPIC_API_KEY in .env, so the AI agents are off")

        with self._lock:   # one call at a time, so two threads can't both squeeze under the cap
            spent = self.spent_today_usd()
            worst = self.worst_case_cost(system, user)
            if spent + worst > self.cfg.daily_spend_cap_usd:
                raise LLMBudgetExceeded(
                    f"Daily AI budget reached: spent US${spent:.4f} of US${self.cfg.daily_spend_cap_usd:.2f}")
            return self._call_unlocked(agent, system, user, output_model)

    def _call_unlocked(self, agent: str, system: str, user: str, output_model: type[T]) -> LLMResult[T]:
        started = time.monotonic()
        try:
            resp = self._client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self.cfg.effort,
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(output_model)},
                },
            )
        except anthropic.AuthenticationError:
            cid = self._record(agent, started, error="Anthropic rejected the API key")
            raise LLMUnavailable("Anthropic rejected the API key (check ANTHROPIC_API_KEY in .env)", cid) from None
        except anthropic.RateLimitError:
            cid = self._record(agent, started, error="rate limited")
            raise LLMUnavailable("Anthropic rate limit hit; try again later", cid) from None
        except anthropic.APIConnectionError as exc:      # includes timeouts
            cid = self._record(agent, started, error=f"connection: {type(exc).__name__}")
            raise LLMUnavailable(f"Couldn't reach the Anthropic API ({type(exc).__name__})", cid) from None
        except anthropic.APIStatusError as exc:
            cid = self._record(agent, started, error=f"HTTP {exc.status_code}")
            raise LLMUnavailable(f"Anthropic API error (HTTP {exc.status_code})", cid) from None

        usage = resp.usage
        tokens = {
            "input": int(getattr(usage, "input_tokens", 0) or 0),
            "output": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        }
        cost = self.cost_usd(tokens["input"], tokens["output"], tokens["cache_write"], tokens["cache_read"])
        request_id = getattr(resp, "_request_id", None)

        error: str | None = None
        parsed: T | None = None
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if resp.stop_reason == "refusal":
            error = "the AI declined to answer"
        elif resp.stop_reason == "max_tokens":
            error = f"the answer was cut off (llm.max_tokens={self.cfg.max_tokens} too small?)"
        elif not text:
            error = "the AI returned no text"
        else:
            try:
                parsed = output_model.model_validate_json(text)
            except ValidationError as exc:
                error = f"the answer didn't match the expected format ({exc.error_count()} problem(s))"

        cid = self._record(agent, started, tokens=tokens, cost=cost, error=error, request_id=request_id)
        if error:
            log.warning("%s: %s", agent, error)
            raise LLMBadOutput(error, cid)
        log.info("%s: %d in / %d out tokens, US$%.5f", agent, tokens["input"], tokens["output"], cost)
        return LLMResult(output=parsed, call_id=cid, cost_usd=cost,
                         input_tokens=tokens["input"], output_tokens=tokens["output"])

    def _record(self, agent: str, started: float, *, tokens: dict | None = None, cost: float = 0.0,
                error: str | None = None, request_id: str | None = None) -> int:
        tokens = tokens or {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        cur = self.db.execute(
            "INSERT INTO llm_calls(ts, agent, model, input_tokens, output_tokens, cache_write_tokens, "
            "cache_read_tokens, cost_usd, latency_ms, ok, error, request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(self.clock.now()), agent, self.cfg.model, tokens["input"], tokens["output"],
             tokens["cache_write"], tokens["cache_read"], cost, int((time.monotonic() - started) * 1000),
             int(error is None), error, request_id),
        )
        return int(cur.lastrowid)
