"""Read-only recognition of dialogue intents selected by the caller."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class Message:
    messageId: str
    role: Literal["user", "iris"]
    text: str


@dataclass(frozen=True)
class Intent:
    intentId: str
    description: str
    positive: tuple[str, ...] = ()
    negative: tuple[str, ...] = ()


@dataclass(frozen=True)
class Observation:
    reply: Message
    history: tuple[Message, ...]
    candidates: tuple[Intent, ...]

    def as_dict(self) -> dict:
        return asdict(self)

    def digest(self) -> str:
        payload = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode()).hexdigest()

    @classmethod
    def from_dict(cls, value: dict) -> "Observation":
        return cls(
            Message(**value["reply"]),
            tuple(Message(**message) for message in value["history"]),
            tuple(Intent(candidate["intentId"], candidate["description"],
                         tuple(candidate["positive"]), tuple(candidate["negative"]))
                  for candidate in value["candidates"]),
        )


class Match(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    intentId: str
    evidence: str


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["matched", "no_match", "uncertain"]
    matches: list[Match]

    def verify(self, observation: Observation) -> None:
        if (self.status == "matched") != bool(self.matches):
            raise ValueError("matches must be present exactly when the status is matched")
        candidates = {candidate.intentId for candidate in observation.candidates}
        seen = set()
        for match in self.matches:
            if match.intentId not in candidates or match.intentId in seen:
                raise ValueError("unexpected or duplicate intent")
            if not match.evidence.strip() or match.evidence not in observation.reply.text:
                raise ValueError("evidence must be an exact excerpt of the current Iris reply")
            seen.add(match.intentId)


@dataclass(frozen=True)
class Recognition:
    decision: Decision | None
    model: str
    promptVersion: str
    latencyMs: int
    attempts: int
    providerModel: str | None = None
    providerRequestId: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.decision.status if self.decision else "unavailable",
            "matches": [match.model_dump() for match in self.decision.matches] if self.decision else [],
            "model": self.model,
            "promptVersion": self.promptVersion,
            "latencyMs": self.latencyMs,
            "attempts": self.attempts,
            "providerModel": self.providerModel,
            "providerRequestId": self.providerRequestId,
            "error": self.error,
        }


class Recognizer(Protocol):
    def recognize(self, observation: Observation) -> Recognition: ...
