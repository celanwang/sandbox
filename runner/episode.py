"""Drive Iris through its public API and capture independently observed evidence."""

import time
from datetime import datetime
from typing import Literal
from uuid import uuid4

import httpx

from monitor import Recognizer
from runner.clients import Iris, Sandbox, pending_approvals
from runner.interactions import LEGACY_REPLY, observe
from runner.scenarios import (
    CANARY_PROMPT, CANARY_TOOL, DEFAULT_QUOTE_ITEMS, ORDER_BUDGET_FEN, ORDER_TOOL, READ_TOOLS, RECOVERY_PROMPT, SCENARIOS,
)
from runner.user import is_question
from runner.verdict import judge, preflight_retained, reported_totals
from sandbox.domains.mcd.world.pricing import PriceError, price_items
from sandbox.episodes import SCHEMA_VERSION, utc_now


def identity(run_id: str, role: str) -> dict:
    return {"userId": f"sandbox-{role}-{run_id}", "sessionId": uuid4().hex}


def error_message(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"{error.request.method} {error.request.url.path}: HTTP {error.response.status_code}"
    return f"{type(error).__name__}: {error}"


def run_episode(name: str, iris: Iris, sandbox: Sandbox, *, seed: int = 0,
                monitor_mode: Literal["off", "shadow", "active"] = "off",
                recognizer: Recognizer | None = None, requested_episode_id: str | None = None) -> dict:
    if monitor_mode != "off" and recognizer is None:
        raise ValueError("monitor mode requires an intent recognizer")
    scenario = SCENARIOS[name]
    actions = scenario.get("interactions", [])
    quote_items = scenario.get("quoteItems", DEFAULT_QUOTE_ITEMS)
    required_actions = {action["actionId"] for action in actions}
    delivered_actions: dict[str, int] = {}
    min_quote_round = 1
    episode_id = requested_episode_id or uuid4().hex
    owns_episode = False
    generation = 1
    scenario_user = None
    evidence = {
        "schemaVersion": SCHEMA_VERSION, "runId": episode_id, "scenario": name, "seed": seed, "startedAt": utc_now(),
        "status": "complete", "errors": {}, "preflight": {}, "transcript": [], "approvals": [],
        "iris": {}, "sandbox": None,
        "monitor": {"mode": monitor_mode, "decisions": [], "deliveredActionIds": []},
    }

    def record_message(round_number: int, role: str, text: str, **fields) -> dict:
        transcript = evidence["transcript"]
        message = {"messageId": f"message-{len(transcript) + 1}", "at": utc_now(), "round": round_number,
                   "role": role, "text": text, **fields}
        transcript.append(message)
        return message

    def chat(user: dict, prompt: str, round_number: int, *, recovery: bool = False,
             follow_up: dict | None = None) -> str:
        message = record_message(round_number, "user", prompt, recovery=recovery)
        if follow_up is not None:
            follow_up["action"]["messageId"] = message["messageId"]
        reply = iris.chat(user, prompt)
        if follow_up is not None:
            follow_up["action"]["acknowledged"] = True
            delivered_actions.update(dict.fromkeys(follow_up["action"]["actionIds"], round_number))
            evidence["monitor"]["deliveredActionIds"] = sorted(delivered_actions)
            evidence["monitor"]["deliveredRounds"] = dict(delivered_actions)
        record_message(round_number, "iris", reply, via="chat", recovery=recovery)
        return reply

    try:
        try:
            sandbox.start(scenario, seed, episode_id)
            owns_episode = True
        except (httpx.TransportError, KeyboardInterrupt):
            # The server may have processed the request before the response was lost.
            owns_episode = True
            raise
        opened = sandbox.snapshot(episode_id, generation=generation)
        if opened["closed"] or opened["round"] != 0 or opened["calls"]:
            owns_episode = False
            raise RuntimeError("runner requires a fresh episode")
        canary = identity(episode_id, "preflight")
        evidence["preflight"]["identity"] = canary
        evidence["preflight"]["prompt"] = CANARY_PROMPT
        evidence["preflight"]["reply"] = iris.chat(canary, CANARY_PROMPT)
        probe = sandbox.snapshot(episode_id, generation=generation)
        calls = probe["calls"]
        verified = (
            any(call["request"]["tool"] == CANARY_TOOL and call["business"]["success"] is True
                and call["transport"]["state"] == "emitted" and call["transport"]["httpStatus"] == 200 for call in calls)
            and all(call["request"]["tool"] in READ_TOOLS for call in calls)
            and not pending_approvals(iris.state(canary))
        )
        evidence["preflight"].update(verified=verified, generation=generation,
                                     requestIds=[call["requestId"] for call in calls])
        if not verified:
            raise RuntimeError("Iris did not complete the read-only probe in this episode")

        scenario_user = identity(episode_id, "scenario")
        evidence["iris"]["identity"] = scenario_user
        prompt = scenario["prompt"]
        follow_up = None
        max_rounds = 3
        for round_number in range(1, max_rounds + 1):
            sandbox.set_round(episode_id, round_number, generation=generation)
            reply = chat(scenario_user, prompt, round_number, follow_up=follow_up)
            follow_up = None
            if scenario.get("quoteAfterInteraction"):
                min_quote_round = max(delivered_actions.values(), default=1)
            for _ in range(6):
                approvals = pending_approvals(iris.state(scenario_user))
                if not approvals:
                    break
                for approval in approvals:
                    approve = (
                        scenario["approve"]
                        and approval["tool"] == ORDER_TOOL
                        and not any(previous["approved"] for previous in evidence["approvals"])
                        and required_actions <= delivered_actions.keys()
                        and matching_quote(sandbox.snapshot(episode_id, generation=generation), approval, reported_totals(reply),
                                           quote_items, min_round=min_quote_round,
                                           budget_fen=scenario.get("budgetFen", ORDER_BUDGET_FEN))
                    )
                    decision = {**approval, "round": round_number, "approved": approve, "at": utc_now()}
                    evidence["approvals"].append(decision)
                    result = iris.confirm(scenario_user, approval, approve)
                    decision["acknowledged"] = True
                    reply = result.get("reply") or ""
                    record_message(round_number, "iris", reply, via="confirm")
            else:
                if pending_approvals(iris.state(scenario_user)):
                    raise RuntimeError("Iris exceeded the approval limit")
            if evidence["approvals"]:
                break
            legacy_would_reply = is_question(reply)
            available_actions = [action for action in actions if action["actionId"] not in delivered_actions]
            if actions and monitor_mode == "active" and not available_actions:
                break
            if available_actions and recognizer is not None and monitor_mode != "off":
                decision, selected = observe(recognizer, evidence["transcript"], available_actions)
                decision["legacyWouldReply"] = legacy_would_reply
                decision["action"] = {"kind": "stop", "actionIds": []}
                evidence["monitor"]["decisions"].append(decision)
                if monitor_mode == "active":
                    status = decision["recognition"]["status"]
                    if status != "matched":
                        decision["action"]["reason"] = status
                        if status in {"uncertain", "unavailable"}:
                            evidence["status"] = f"monitor_{status}"
                        break
                    if round_number == max_rounds:
                        decision["action"]["reason"] = "round_limit"
                        break
                    decision["action"] = {"kind": "scripted_reply", "actionIds": decision["proposedActionIds"]}
                    follow_up = decision
                    prompt = " ".join(action["reply"] for action in selected)
                    continue
                if legacy_would_reply and round_number < max_rounds:
                    decision["action"] = {"kind": "legacy_reply", "actionIds": []}
                    follow_up = decision
                else:
                    decision["action"]["reason"] = "round_limit" if legacy_would_reply else "legacy_no_question"
            if not legacy_would_reply or round_number == max_rounds:
                break
            prompt = LEGACY_REPLY
        if scenario.get("faults") and any(decision["approved"] for decision in evidence["approvals"]):
            sandbox.set_round(episode_id, round_number + 1, generation=generation)
            chat(scenario_user, RECOVERY_PROMPT, round_number + 1, recovery=True)
            if pending_approvals(iris.state(scenario_user)):
                evidence["recoveryRequestedApproval"] = True
    except KeyboardInterrupt:
        evidence["status"] = "interrupted"
        evidence["errors"]["execution"] = "runner interrupted"
    except Exception as error:
        evidence["status"] = "execution_error"
        evidence["errors"]["execution"] = error_message(error)

    try:
        if scenario_user is not None and evidence["status"] != "interrupted":
            for key, reader in (("state", iris.state), ("trace", iris.trace)):
                try:
                    evidence["iris"][key] = reader(scenario_user)
                except Exception as error:
                    evidence["errors"][f"iris.{key}"] = error_message(error)
            if "trace" in evidence["iris"] and not evidence["iris"]["trace"]:
                evidence["errors"]["iris.trace"] = "Iris returned no trace events for the scenario"
    except KeyboardInterrupt:
        evidence["status"] = "interrupted"
        evidence["errors"]["execution"] = "runner interrupted during Iris evidence collection"
    finally:
        if owns_episode:
            try:
                evidence["sandbox"] = close_when_idle(sandbox, episode_id, generation)
            except Exception as error:
                evidence["errors"]["sandbox.close"] = error_message(error)
                try:
                    evidence["sandbox"] = sandbox.snapshot(episode_id)
                except Exception as snapshot_error:
                    evidence["errors"]["sandbox.snapshot"] = error_message(snapshot_error)

    if evidence["errors"] and evidence["status"] == "complete":
        evidence["status"] = "capture_error"
    snapshot = evidence["sandbox"]
    if evidence["preflight"].get("verified") and not preflight_retained(evidence):
        if evidence["status"] == "complete":
            evidence["status"] = "capture_error"
        evidence["errors"]["sandbox.preflight"] = "verified probe is missing from the closed episode generation"
    trace = evidence["iris"].get("trace", [])
    evidence["provenance"] = {
        "scenario": scenario,
        "irisTrace": {
            "events": len(trace),
            "possibleEventLimit": len(trace) >= 500,
            "truncatedTextObserved": any("…[已截断]" in str(event.get("detail", "")) for event in trace),
            "observedModels": sorted({event["node"] for event in trace
                                      if event.get("type") == "MODEL_CALL" and event.get("node")}),
        },
        "mcpRequestsWithMetadata": sum(bool(event["request"]["meta"]) for event in snapshot["calls"]) if snapshot else 0,
    }
    approved_store = next((str(row["arguments"].get("storeCode")) for row in evidence["approvals"]
                           if row["approved"]), None)
    quote = (last_quote(snapshot, quote_items, min_round=min_quote_round, store_code=approved_store)
             if snapshot and evidence["status"] == "complete" else None)
    evidence["verdict"] = judge(evidence, quote)
    evidence["finishedAt"] = utc_now()
    return evidence


def close_when_idle(sandbox: Sandbox, episode_id: str, generation: int) -> dict:
    deadline = time.monotonic() + 5
    while True:
        try:
            return sandbox.close(episode_id, generation=generation)
        except httpx.HTTPStatusError as error:
            if (error.response.status_code != 409 or "generation changed" in error.response.text
                    or time.monotonic() >= deadline):
                raise
            time.sleep(0.1)


def matching_quote(snapshot: dict, approval: dict, visible_totals: set[int],
                   quote_items: list[dict] = DEFAULT_QUOTE_ITEMS, *, min_round: int = 1,
                   budget_fen: int = ORDER_BUDGET_FEN) -> bool:
    if snapshot["closed"] or not isinstance(approval["arguments"], dict):
        return False
    args = approval["arguments"]
    state = snapshot["state"]
    store = state["stores"].get(str(args.get("storeCode")))
    address = state["addresses"].get(str(args.get("addressId")))
    if (not store or not address or address["tag"] != "公司" or args.get("reservationDate")
            or address["addressId"] not in store["deliverTo"]
            or str(args.get("beType")) != "2" or str(args.get("orderType")) != "2"):
        return False
    quote = last_quote(snapshot, quote_items, min_round=min_round, store_code=store["storeCode"])
    if not quote or quote["priceFen"] > budget_fen or visible_totals != {quote["priceFen"]}:
        return False
    world = {**state, "coupons": list(state["coupons"].values())}
    try:
        now = datetime.fromisoformat(state["clock"]["now"])
        price = price_items(world, store, args.get("items"), now, bool(args.get("needTableware")))
    except (PriceError, TypeError, AttributeError):
        return False
    return price.content_key == quote["contentKey"] and price.price == quote["priceFen"]


def last_quote(snapshot: dict, quote_items: list[dict] = DEFAULT_QUOTE_ITEMS, *, min_round: int = 1,
               store_code: str | None = None) -> dict | None:
    state = snapshot["state"]
    world = {**state, "coupons": list(state["coupons"].values()), "soldOut": {}}
    now = datetime.fromisoformat(state["clock"]["now"])
    quotes = sorted(state["quotes"].values(), key=lambda value: value["callSeq"], reverse=True)
    for quote in quotes:
        if quote["round"] < min_round or store_code is not None and quote["storeCode"] != store_code:
            continue
        store = state["stores"][quote["storeCode"]]
        expected = price_items(world, store, quote_items, now)
        if quote["contentKey"] == expected.content_key and quote["priceFen"] == expected.price:
            return quote
    return None
