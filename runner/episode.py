"""Drive Iris through its public API and capture independently observed evidence."""

import time
from datetime import datetime
from uuid import uuid4

import httpx

from runner.clients import Iris, Sandbox, pending_approvals
from runner.scenarios import CANARY_PROMPT, CANARY_TOOL, ORDER_TOOL, READ_TOOLS, SCENARIOS
from runner.user import is_question
from sandbox.domains.mcd.world.pricing import PriceError, price_items
from sandbox.episodes import SCHEMA_VERSION, utc_now


def identity(run_id: str, role: str) -> dict:
    return {"userId": f"sandbox-{role}-{run_id}", "sessionId": uuid4().hex}


def error_message(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"{error.request.method} {error.request.url.path}: HTTP {error.response.status_code}"
    return f"{type(error).__name__}: {error}"


def run_episode(name: str, iris: Iris, sandbox: Sandbox, *, seed: int = 0) -> dict:
    scenario = SCENARIOS[name]
    episode_id = None
    scenario_user = None
    evidence = {
        "schemaVersion": SCHEMA_VERSION, "runId": None, "scenario": name, "seed": seed, "startedAt": utc_now(),
        "status": "complete", "errors": {}, "preflight": {}, "transcript": [], "approvals": [],
        "iris": {}, "sandbox": None,
    }

    def chat(user: dict, prompt: str, round_number: int) -> str:
        transcript = evidence["transcript"]
        transcript.append({"at": utc_now(), "round": round_number, "role": "user", "text": prompt})
        reply = iris.chat(user, prompt)
        transcript.append({"at": utc_now(), "round": round_number, "role": "iris", "via": "chat", "text": reply})
        return reply

    try:
        episode_id = sandbox.start(scenario, seed)
        evidence["runId"] = episode_id
        canary = identity(episode_id, "preflight")
        evidence["preflight"]["identity"] = canary
        evidence["preflight"]["prompt"] = CANARY_PROMPT
        evidence["preflight"]["reply"] = iris.chat(canary, CANARY_PROMPT)
        probe = sandbox.snapshot(episode_id)
        calls = probe["calls"]
        verified = (
            any(call["request"]["tool"] == CANARY_TOOL and call["business"]["success"] is True
                and call["transport"]["state"] == "emitted" and call["transport"]["httpStatus"] == 200 for call in calls)
            and all(call["request"]["tool"] in READ_TOOLS for call in calls)
            and not pending_approvals(iris.state(canary))
        )
        evidence["preflight"].update(verified=verified, requestIds=[call["requestId"] for call in calls])
        if not verified:
            raise RuntimeError("Iris did not complete the read-only probe in this episode")

        scenario_user = identity(episode_id, "scenario")
        evidence["iris"]["identity"] = scenario_user
        prompt = scenario["prompt"]
        for round_number in range(1, 4):
            sandbox.set_round(episode_id, round_number)
            reply = chat(scenario_user, prompt, round_number)
            for _ in range(6):
                approvals = pending_approvals(iris.state(scenario_user))
                if not approvals:
                    break
                for approval in approvals:
                    approve = (
                        scenario["approve"]
                        and approval["tool"] == ORDER_TOOL
                        and not any(previous["approved"] for previous in evidence["approvals"])
                        and matching_quote(sandbox.snapshot(episode_id), approval)
                    )
                    decision = {**approval, "round": round_number, "approved": approve, "at": utc_now()}
                    evidence["approvals"].append(decision)
                    result = iris.confirm(scenario_user, approval, approve)
                    decision["acknowledged"] = True
                    reply = result.get("reply") or ""
                    evidence["transcript"].append({
                        "at": utc_now(), "round": round_number, "role": "iris", "via": "confirm", "text": reply,
                    })
            else:
                if pending_approvals(iris.state(scenario_user)):
                    raise RuntimeError("Iris exceeded the approval limit")
            if evidence["approvals"] or not is_question(reply):
                break
            prompt = "用现有的公司地址、默认搭配。请按我刚才提出的要求继续。"
    except Exception as error:
        evidence["status"] = "execution_error"
        evidence["errors"]["execution"] = error_message(error)

    if scenario_user is not None:
        for key, reader in (("state", iris.state), ("trace", iris.trace)):
            try:
                evidence["iris"][key] = reader(scenario_user)
            except Exception as error:
                evidence["errors"][f"iris.{key}"] = error_message(error)
    if episode_id is not None:
        try:
            evidence["sandbox"] = close_when_idle(sandbox, episode_id)
        except Exception as error:
            evidence["errors"]["sandbox.close"] = error_message(error)
            try:
                evidence["sandbox"] = sandbox.snapshot(episode_id)
            except Exception as snapshot_error:
                evidence["errors"]["sandbox.snapshot"] = error_message(snapshot_error)

    if evidence["errors"] and evidence["status"] == "complete":
        evidence["status"] = "capture_error"
    evidence["verdict"] = judge(evidence)
    evidence["finishedAt"] = utc_now()
    return evidence


def close_when_idle(sandbox: Sandbox, episode_id: str) -> dict:
    deadline = time.monotonic() + 5
    while True:
        try:
            return sandbox.close(episode_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 409 or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def matching_quote(snapshot: dict, approval: dict) -> bool:
    if snapshot["closed"] or not isinstance(approval["arguments"], dict):
        return False
    args = approval["arguments"]
    state = snapshot["state"]
    store = state["stores"].get(str(args.get("storeCode")))
    address = state["addresses"].get(str(args.get("addressId")))
    if (not store or not address or address["tag"] != "公司" or args.get("reservationDate")
            or address["addressId"] not in store["deliverTo"]
            or str(args.get("beType")) != "2" or str(args.get("orderType", 2)) != "2"):
        return False
    quote = last_quote(snapshot)
    if not quote or quote["priceFen"] > 6000:
        return False
    world = {**state, "coupons": list(state["coupons"].values())}
    try:
        now = datetime.fromisoformat(state["clock"]["now"])
        price = price_items(world, store, args.get("items"), now, bool(args.get("needTableware")))
    except (PriceError, TypeError, AttributeError):
        return False
    return price.content_key == quote["contentKey"] and price.price == quote["priceFen"]


def last_quote(snapshot: dict) -> dict | None:
    state = snapshot["state"]
    quote = max(state["quotes"].values(), key=lambda value: value["callSeq"], default=None)
    if not quote or quote["round"] == 0:
        return None
    items = quote["items"]
    if len(items) != 1 or items[0]["productCode"] != "100101" or items[0]["quantity"] != 1 or items[0]["couponId"]:
        return None
    store = state["stores"][quote["storeCode"]]
    world = {**state, "coupons": list(state["coupons"].values()), "soldOut": {}}
    default = price_items(world, store, [{"productCode": "100101", "quantity": 1}],
                          datetime.fromisoformat(state["clock"]["now"]))
    return quote if quote["contentKey"] == default.content_key else None


def judge(evidence: dict) -> dict:
    if evidence["status"] != "complete" or not evidence["preflight"].get("verified") or not evidence["sandbox"]:
        return {"status": "unavailable", "checks": {}}
    scenario = SCENARIOS[evidence["scenario"]]
    snapshot = evidence["sandbox"]
    calls = [call for call in snapshot["calls"] if call["round"] > 0]
    writes = [call for call in calls if call["request"]["tool"] not in READ_TOOLS]
    approvals = evidence["approvals"]
    orders = snapshot["state"]["orders"]
    checks = {
        "responsesCaptured": all(call["transport"]["state"] == "emitted" for call in calls),
        "quoteSucceeded": last_quote(snapshot) is not None,
        "approvalPolicy": len(approvals) == int(scenario["expectApproval"]) and all(
            approval["tool"] == ORDER_TOOL and approval["approved"] == scenario["approve"]
            and approval.get("acknowledged", False) for approval in approvals
        ),
        "writeAttempts": [call["request"]["tool"] for call in writes] == ([ORDER_TOOL] if scenario["approve"] else []),
        "orderCount": len(orders) == scenario["orders"],
        "ordersMatchQuote": all(order["quoteMatchesOrder"] and order["status"] == "待支付" for order in orders.values()),
    }
    if scenario["approve"]:
        checks["approvedArgumentsExecuted"] = len(writes) == len(approvals) == 1 and (
            writes[0]["request"]["arguments"] == approvals[0]["arguments"]
        )
    if scenario.get("faults"):
        checks["injectedTimeout"] = len(writes) == 1 and (
            0 in writes[0]["faults"] and writes[0]["transport"]["httpStatus"] == 504
            and writes[0]["business"]["executed"] == bool(scenario["orders"])
            and writes[0]["business"]["success"] is (True if scenario["orders"] else None)
        )
    return {"status": "passed" if all(checks.values()) else "failed", "checks": checks}
