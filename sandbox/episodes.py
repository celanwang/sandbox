"""Domain-independent episode lifecycle and execution evidence."""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
HTTP_FAULTS = {
    "TIMEOUT_COMMITTED": 504,
    "TIMEOUT_NOT_COMMITTED": 504,
    "CONNECTION_FAIL": 502,
    "AUTH_401": 401,
}
log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Fault(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    tool: str = "*"
    nth: int = Field(default=1, ge=1)
    at: Literal["start", "call"] = "call"
    target: str | None = None
    persist: bool = False
    mode: Literal["http", "rpc-error"] = "http"
    delaySeconds: float = Field(default=0, ge=0, allow_inf_nan=False)


@dataclass(frozen=True)
class Outcome:
    success: bool | None
    result: dict | str
    text: str


class Domain(Protocol):
    name: str
    tools: list[dict]
    business_faults: frozenset[str]

    def scenes(self) -> list[str]: ...
    def open(self, scene: str, seed: int) -> dict: ...
    def state(self, world: dict) -> dict[str, dict]: ...
    def fault(self, world: dict, fault: Fault) -> None: ...
    def call(self, episode: Episode, tool: str, arguments: dict) -> Outcome: ...


class EpisodeConflict(RuntimeError):
    pass


class Episode:
    def __init__(self, domain: Domain, scene: str, seed: int, faults: list[Fault]):
        self.episode_id = uuid4().hex
        self.domain = domain
        self.scene_id = scene
        self.seed = seed
        self.faults = tuple(faults)
        tool_schema = json.dumps(domain.tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.tool_schema_sha256 = hashlib.sha256(tool_schema.encode()).hexdigest()
        self.lock = RLock()
        self.generation = 0
        self.closed = False
        self.in_flight = 0

        tool_names = {tool["name"] for tool in domain.tools}
        world = domain.open(scene, seed)
        self.start_faults = []
        for index, fault in enumerate(self.faults):
            if fault.tool != "*" and fault.tool not in tool_names:
                raise ValueError(f"unknown fault tool: {fault.tool}")
            if fault.type in HTTP_FAULTS:
                if fault.at != "call" or fault.target is not None:
                    raise ValueError("transport faults apply to calls and take no target")
            elif fault.type in domain.business_faults:
                if fault.mode != "http" or fault.delaySeconds:
                    raise ValueError("business faults take neither a transport mode nor a response delay")
                if fault.at == "start" and (fault.tool != "*" or fault.nth != 1 or fault.persist):
                    raise ValueError("start faults take no tool selector, ordinal, or persistence")
                # Validate scheduled targets against the fixture before accepting the run.
                domain.fault(deepcopy(world), fault)
                if fault.at == "start":
                    domain.fault(world, fault)
                    self.start_faults.append(index)
            else:
                raise ValueError(f"unknown fault: {fault.type}")
        self.initial_world = world
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.ensure_idle()
            if self.closed:
                raise EpisodeConflict("episode is closed")
            self.generation += 1
            self.world = deepcopy(self.initial_world)
            self.seq = self.round = 0
            self.calls: list[dict] = []
            self.counters: dict[str, int] = {}
            self.fired = set(self.start_faults)

    def ensure_idle(self) -> None:
        if self.in_flight:
            raise EpisodeConflict("episode has requests in flight")

    def _due(self, tool: str, nth: int) -> list[int]:
        due = []
        for index, fault in enumerate(self.faults):
            if fault.at != "call" or fault.tool not in ("*", tool):
                continue
            count = self.seq + 1 if fault.tool == "*" else nth
            if (index not in self.fired and count == fault.nth) or (fault.persist and count >= fault.nth):
                due.append(index)
        return due

    def invoke(self, tool: str, arguments: dict, rpc_id: int | str, meta: dict | None) -> tuple[dict, Fault | None]:
        with self.lock:
            if self.closed:
                raise EpisodeConflict("episode is closed")
            nth = self.counters.get(tool, 0) + 1
            due = self._due(tool, nth)
            transport = next((index for index in due if self.faults[index].type in HTTP_FAULTS), None)
            override = self.faults[transport] if transport is not None else None
            execute = override is None or override.type == "TIMEOUT_COMMITTED"
            business_faults = [index for index in due if self.faults[index].type in self.domain.business_faults]
            planned_status = (200 if override.mode == "rpc-error" else HTTP_FAULTS[override.type]) if override else None
            before = deepcopy(self.domain.state(self.world))
            event = {
                "schemaVersion": SCHEMA_VERSION,
                "type": "tool.call",
                "runId": self.episode_id,
                "generation": self.generation,
                "requestId": uuid4().hex,
                "requestedAt": utc_now(),
                "seq": self.seq + 1,
                "round": self.round,
                "nth": nth,
                "request": {"rpcId": rpc_id, "tool": tool, "arguments": deepcopy(arguments), "meta": deepcopy(meta)},
                "faults": [] if transport is None else [transport],
                "skippedFaults": [
                    {"index": index, "reason": "transport_fault_selected"}
                    for index in due if index != transport and self.faults[index].type in HTTP_FAULTS
                ] + [
                    {"index": index, "reason": "business_not_attempted"}
                    for index in business_faults if not execute
                ],
                "business": {"executed": False, "success": None, "result": None},
                "changes": [],
                "transport": {"state": "pending", "httpStatus": None, "body": None, "completedAt": None,
                              "plannedHttpStatus": planned_status},
            }
            if execute:
                for index in business_faults:
                    self.domain.fault(self.world, self.faults[index])
                event["faults"].extend(business_faults)
                try:
                    outcome = self.domain.call(self, tool, arguments)
                    event["business"] = {
                        "executed": True, "success": outcome.success, "result": deepcopy(outcome.result),
                        "text": outcome.text,
                    }
                except Exception:
                    # Keep the evidence if a domain fails after touching state.
                    log.exception("Domain %s failed calling %s", self.domain.name, tool)
                    event["business"] = {"executed": True, "success": None, "result": None, "error": "domain error"}

            after = self.domain.state(self.world)
            for collection in sorted(before.keys() | after.keys()):
                old_values, new_values = before.get(collection, {}), after.get(collection, {})
                for key in sorted(old_values.keys() | new_values.keys()):
                    old, new = old_values.get(key), new_values.get(key)
                    if old != new:
                        event["changes"].append({
                            "collection": collection, "id": key, "before": old, "after": deepcopy(new),
                        })
            self.fired.update(event["faults"])
            self.seq += 1
            self.counters[tool] = nth
            self.calls.append(event)
            return event, override

    def finish(self, event: dict, status: int | None, body: str | None, state: str) -> None:
        with self.lock:
            event["transport"].update(state=state, httpStatus=status, body=body, completedAt=utc_now())

    def snapshot(self) -> dict:
        with self.lock:
            return deepcopy({
                "schemaVersion": SCHEMA_VERSION,
                "runId": self.episode_id,
                "episodeId": self.episode_id,
                "generation": self.generation,
                "domain": self.domain.name,
                "toolSchemaSha256": self.tool_schema_sha256,
                "scene": self.scene_id,
                "seed": self.seed,
                "closed": self.closed,
                "round": self.round,
                "faultPlan": [fault.model_dump() for fault in self.faults],
                "startFaults": self.start_faults,
                "initialState": self.domain.state(self.initial_world),
                "state": self.domain.state(self.world),
                "calls": self.calls,
            })

    def events(self) -> list[dict]:
        with self.lock:
            return deepcopy(self.calls)


class EpisodeStore:
    def __init__(self):
        self.episodes: dict[str, Episode] = {}
        self.bindings: dict[str, Episode] = {}
        self.lock = RLock()

    def create(self, token: str, domain: Domain, scene: str, seed: int, faults: list[Fault]) -> Episode:
        with self.lock:
            if token in self.bindings:
                raise EpisodeConflict("token already has an active episode")
            episode = Episode(domain, scene, seed, faults)
            self.episodes[episode.episode_id] = self.bindings[token] = episode
            return episode

    def get(self, episode_id: str) -> Episode | None:
        with self.lock:
            return self.episodes.get(episode_id)

    def bound(self, token: str) -> Episode | None:
        with self.lock:
            return self.bindings.get(token)

    def admit(self, token: str) -> Episode | None:
        with self.lock:
            episode = self.bindings.get(token)
            if episode is not None:
                with episode.lock:
                    episode.in_flight += 1
            return episode

    def close(self, episode: Episode) -> dict:
        with self.lock, episode.lock:
            episode.ensure_idle()
            episode.closed = True
            for token, bound in tuple(self.bindings.items()):
                if bound is episode:
                    del self.bindings[token]
                    break
            return episode.snapshot()
