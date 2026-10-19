"""The exact shapes the AI agents must answer in.

These are sent to Claude as a JSON schema, and every answer is checked
against them before the bot uses it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _one_line(v: str, limit: int = 300) -> str:
    v = " ".join(str(v).split())
    return v[: limit - 1] + "…" if len(v) > limit else v


class CoinSentiment(BaseModel):
    coin: str = Field(description="Coin code exactly as given, e.g. BTC")
    sentiment: float = Field(description="News mood for this coin from -1 (very negative) to +1 (very positive); 0 = neutral or no real news")
    reason: str = Field(description="One short line explaining the score")
    major_event: bool = Field(description="True only for a serious event that makes trading this coin risky right now: hack/exploit of the coin or its main network, exchange outage or insolvency, regulatory action against it, delisting, depeg")
    event_summary: str = Field(description="One line describing the major event, or an empty string if major_event is false")

    @field_validator("coin", mode="before")
    @classmethod
    def _upper(cls, v):
        return str(v).strip().upper()

    @field_validator("sentiment", mode="before")
    @classmethod
    def _clamp(cls, v):
        v = float(v)
        return max(-1.0, min(1.0, v))

    @field_validator("reason", "event_summary", mode="before")
    @classmethod
    def _line(cls, v):
        return _one_line(v)


class NewsAssessment(BaseModel):
    coins: list[CoinSentiment] = Field(description="One entry per coin you were asked about")


def _clamp_confidence(v) -> int:
    return int(max(0, min(100, round(float(v)))))


class EntryProposal(BaseModel):
    coin: str = Field(description="Coin code exactly as given")
    decision: Literal["BUY", "NO_TRADE"] = Field(description="BUY to propose a trade, NO_TRADE to pass")
    entry_aud: float = Field(description="Expected entry price in AUD (about the current CoinSpot ask). 0 if NO_TRADE")
    stop_loss_aud: float = Field(description="Stop-loss price in AUD, below the entry. 0 if NO_TRADE")
    take_profit_aud: float = Field(description="Take-profit price in AUD, above the entry. 0 if NO_TRADE")
    confidence: int = Field(description="0-100: how confident you are this trade reaches its take-profit before its stop")
    reasoning: str = Field(description="2-3 sentences: why this trade (or why not)")

    @field_validator("coin", mode="before")
    @classmethod
    def _coin(cls, v):
        return str(v).strip().upper()

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        return _clamp_confidence(v)

    @field_validator("reasoning", mode="before")
    @classmethod
    def _reason(cls, v):
        return _one_line(v, 600)


class ExitAdvice(BaseModel):
    position_id: str = Field(description="The position id exactly as given")
    action: Literal["HOLD", "SELL", "TIGHTEN_STOP"] = Field(
        description="HOLD = keep it as is; SELL = close now; TIGHTEN_STOP = raise the stop-loss")
    new_stop_aud: float = Field(description="New, higher stop-loss in AUD if TIGHTEN_STOP, otherwise 0")
    confidence: int = Field(description="0-100: how confident you are in this action")
    reasoning: str = Field(description="1-2 sentences")

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        return _clamp_confidence(v)

    @field_validator("position_id", mode="before")
    @classmethod
    def _pid(cls, v):
        return str(v).strip()

    @field_validator("reasoning", mode="before")
    @classmethod
    def _reason(cls, v):
        return _one_line(v, 400)


class ExitReview(BaseModel):
    positions: list[ExitAdvice] = Field(description="One entry per position you were asked about")


class SkepticVerdict(BaseModel):
    arguments_against: list[str] = Field(description="The strongest 2-4 concrete reasons this could go wrong")
    vote: Literal["BUY", "SELL", "WAIT", "SKIP"] = Field(
        description="For a buy proposal: BUY (agree), WAIT (not yet) or SKIP (reject). "
                    "For a sell proposal: SELL (agree), WAIT (hold for now) or SKIP (reject, keep the position)")
    reasoning: str = Field(description="1-2 sentences explaining the vote")

    @field_validator("arguments_against", mode="before")
    @classmethod
    def _args(cls, v):
        return [_one_line(a, 300) for a in (v or []) if str(a).strip()][:6]

    @field_validator("reasoning", mode="before")
    @classmethod
    def _reason(cls, v):
        return _one_line(v, 400)


class PropReview(BaseModel):
    verdict: Literal["approve", "veto"] = Field(
        description="approve = let the rule-based buy go ahead; veto = skip it (only for a concrete, serious reason)")
    confidence: int = Field(description="How sure you are, 0-100")
    reason: str = Field(description="One short line: the main reason for the verdict")

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_conf(cls, v):
        return max(0, min(100, int(round(float(v)))))

    @field_validator("reason", mode="before")
    @classmethod
    def _line(cls, v):
        return _one_line(v)
