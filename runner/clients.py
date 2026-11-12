"""HTTP clients for an independently running Iris and Sandbox."""

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

    def start(self, scenario: dict, seed: int) -> str:
        return self.http.post("/episodes", json={
            "token": self.token, "domain": "mcd", "scene": "lunch", "seed": seed,
            "faults": scenario.get("faults", []),
        }).raise_for_status().json()["episodeId"]

    def set_round(self, episode_id: str, round_number: int) -> None:
        self.http.post(f"/episodes/{episode_id}/round", json={"round": round_number}).raise_for_status()

    def snapshot(self, episode_id: str) -> dict:
        return self.http.get(f"/episodes/{episode_id}/snapshot").raise_for_status().json()

    def close(self, episode_id: str) -> dict:
        return self.http.post(f"/episodes/{episode_id}/close").raise_for_status().json()
