"""Shared plumbing for the AI agents: call the model, log every output."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

from desk.clock import Clock, iso
from desk.config import Config
from desk.db import Database
from desk.llm import LLMClient, LLMError
from desk.prompts import load_prompt

log = logging.getLogger("desk.agents")

T = TypeVar("T", bound=BaseModel)


@dataclass
class AgentReply(Generic[T]):
    output: T | None
    error: str | None
    call_id: int | None
    user_message: str

    @property
    def ok(self) -> bool:
        return self.output is not None


class Agent:
    name = "agent"          # used in the database
    prompt_file = ""        # prompts/<prompt_file>.md

    def __init__(self, config: Config, llm: LLMClient, db: Database, clock: Clock):
        self.config = config
        self.llm = llm
        self.db = db
        self.clock = clock

    def ask(self, user: str, output_model: type[T], *, coin: str | None, cycle_id: str | None) -> AgentReply[T]:
        """One AI call. Never raises: failures come back as AgentReply.error."""
        try:
            res = self.llm.call(self.name, load_prompt(self.prompt_file), user, output_model)
        except Exception as exc:     # LLMError, missing prompt file, anything
            if not isinstance(exc, LLMError):
                log.exception("%s failed", self.name)
            reply = AgentReply(None, str(exc), getattr(exc, "call_id", None), user)
        else:
            reply = AgentReply(res.output, None, res.call_id, user)
        self.db.execute(
            "INSERT INTO agent_outputs(ts, cycle_id, agent, coin, ok, input, output, error, llm_call_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (iso(self.clock.now()), cycle_id, self.name, coin, int(reply.ok), user,
             json.dumps(reply.output.model_dump()) if reply.output is not None else None,
             reply.error, reply.call_id),
        )
        return reply
