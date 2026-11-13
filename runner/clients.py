"""HTTP clients for an independently running Iris and Sandbox."""

from uuid import uuid4

import httpx


class Iris:
    def __init__(self, http: httpx.Client):
        self.http = http

    def chat(self, identity: dict, prompt: str) -> str:
        return self.http.post("/chat", json={**identity, "prompt": prompt}).raise_for_status().text

    def state(self, identity: dict) -> dict:
        state = self.http.get("/agent/state", params=identity).raise_for_status().json()
        if not isinstance(state, dict) or not isinstance(state.get("tasks"), list):
            raise ValueError("Iris returned an invalid agent state")
        return state

    def confirm(self, identity: dict, approval: dict, approved: bool) -> dict:
        return self.http.post("/agent/confirm", json={
            **identity,
            "taskId": approval["taskId"],
            "approvalId": approval["approvalId"],
            "approved": approved,
        }).raise_for_status().json()

    def trace(self, identity: dict) -> list[dict]:
        events = self.http.get("/trace", params=identity).raise_for_status().json()
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            raise ValueError("Iris returned an invalid trace")
        return events


def pending_approvals(state: dict) -> list[dict]:
    pending = []
    for task in state.get("tasks") or []:
        document = task.get("content") or {}
        operations = document.get("operations") or {}
        for call_id, approval in (document.get("approvals") or {}).items():
            if approval.get("status") == "PENDING" and call_id not in operations:
                pending.append({
                    "taskId": task["taskId"], "approvalId": approval["id"], "callId": call_id,
                    "tool": approval["tool"], "arguments": approval["arguments"],
                })
    return pending


class Sandbox:
    def __init__(self, http: httpx.Client, token: str):
        self.http = http
        self.token = token

    def start(self, scenario: dict, seed: int, episode_id: str | None = None) -> str:
        episode_id = episode_id or uuid4().hex
        payload = {
            "episodeId": episode_id,
            "token": self.token, "domain": "mcd", "scene": "lunch", "seed": seed,
            "faults": scenario.get("faults", []),
        }
        for attempt in range(2):
            try:
                result = self.http.post("/episodes", json=payload).raise_for_status().json()
                if result["episodeId"] != episode_id:
                    raise ValueError("Sandbox returned a different episode ID")
                return episode_id
            except httpx.TransportError:
                if attempt:
                    raise

    def set_round(self, episode_id: str, round_number: int, *, generation: int | None = None) -> None:
        payload = {"round": round_number}
        if generation is not None:
            payload["generation"] = generation
        self.http.post(f"/episodes/{episode_id}/round", json=payload).raise_for_status()

    def snapshot(self, episode_id: str, *, generation: int | None = None) -> dict:
        params = {"generation": generation} if generation is not None else None
        return self.http.get(f"/episodes/{episode_id}/snapshot", params=params).raise_for_status().json()

    def close(self, episode_id: str, *, generation: int | None = None) -> dict:
        params = {"generation": generation} if generation is not None else None
        return self.http.post(f"/episodes/{episode_id}/close", params=params).raise_for_status().json()
