"""任务 spec 加载与判定的离线用例。"""
import pytest
from pydantic import ValidationError

from runner.episode import _task_from_view
from runner.tasks import CHECK_IDS, TASKS_DIR, TaskSpec, legacy_view, load_tasks
from runner.verdict import judge

RUN_ID = "run-" + "0" * 28
ORDER_ID = "1030612264955701724958242424"
QUOTE = {"callSeq": 4, "round": 1, "storeCode": "100001", "contentKey": "{}", "priceFen": 5390}


def tool_call(seq, round_number, tool, request_id, arguments=None):
    return {
        "schemaVersion": 2, "type": "tool.call", "runId": RUN_ID, "generation": 1,
        "requestId": request_id, "seq": seq, "round": round_number, "nth": 1,
        "request": {"rpcId": seq, "tool": tool, "arguments": arguments or {}, "meta": None},
        "faults": [], "skippedFaults": [],
        "business": {"executed": True, "success": True, "result": {}, "text": ""},
        "changes": [],
        "transport": {"state": "emitted", "httpStatus": 200, "body": "",
                      "completedAt": "2026-11-10T12:00:00+00:00", "plannedHttpStatus": None},
    }


def approved_row(approved: bool = True) -> dict:
    return {"taskId": "task-1", "callId": "call-1", "tool": "create-order",
            "arguments": {"storeCode": "100001"}, "approved": approved, "acknowledged": True}


def evidence_for(task: TaskSpec, reply: str, *, approvals: list[dict] | str = "default") -> dict:
    if approvals == "default":
        approvals = [approved_row()] if task.script.approve else []
    writes = bool(approvals and approvals[0]["approved"])
    calls = [tool_call(1, 0, "delivery-query-addresses", "req-canary"),
             tool_call(2, 1, "calculate-price", "req-quote")]
    orders = {}
    operations = {}
    if writes:
        calls.append(tool_call(3, 1, "create-order", "req-order", {"storeCode": "100001"}))
        orders = {ORDER_ID: {"orderId": ORDER_ID, "status": "待支付", "quoteMatchesOrder": True}}
        operations = {"call-1": {"tool": "create-order", "status": "SUCCEEDED", "businessId": ORDER_ID}}
    return {
        "schemaVersion": 2, "runId": RUN_ID, "status": "complete", "errors": {},
        "scenario": task.taskId, "taskId": task.taskId,
        "preflight": {"verified": True, "generation": 1, "requestIds": ["req-canary"]},
        "transcript": [
            {"messageId": "message-1", "at": "2026-11-10T12:00:00+00:00", "round": 1,
             "role": "user", "text": task.script.prompt},
            {"messageId": "message-2", "at": "2026-11-10T12:00:10+00:00", "round": 1,
             "role": "iris", "text": "总价：53.9 元", "via": "chat"},
            {"messageId": "message-3", "at": "2026-11-10T12:00:20+00:00", "round": 1,
             "role": "iris", "text": reply, "via": "confirm"},
        ],
        "approvals": approvals,
        "iris": {"state": {"tasks": [{"taskId": "task-1", "content": {"operations": operations}}]}},
        "sandbox": {"schemaVersion": 2, "runId": RUN_ID, "episodeId": RUN_ID, "generation": 1,
                    "closed": True, "calls": calls,
                    "state": {"orders": orders, "quotes": {"4": QUOTE}, "addresses": {}, "stores": {}}},
        "monitor": {"deliveredRounds": {}, "deliveredActionIds": []},
    }


@pytest.fixture(scope="module")
def tasks():
    return load_tasks(TASKS_DIR)


def test_all_tasks_load(tasks):
    assert set(tasks) == {"order", "quote", "deny", "budget-exceeded",
                          "timeout-before", "timeout-after", "clarify-address", "clarify-drink"}
    assert tasks["clarify-address"].script.interactions[0].actionId == "provide_company_address"
    assert tasks["timeout-after"].faults[0].type == "TIMEOUT_COMMITTED"
    assert tasks["budget-exceeded"].script.budgetFen == 5000


@pytest.mark.parametrize("task_set", sorted(
    path for path in TASKS_DIR.iterdir() if path.is_dir()), ids=lambda path: path.name)
def test_task_set_directory_loads_its_tasks(task_set):
    assert (task_set / "task-set.json").is_file()
    loaded = load_tasks(task_set)
    assert len(loaded) == 13
    assert loaded["s0-address-query"].expect.quote is False
    assert loaded["s0-order-create"].script.approvals[0].call == "create"


def test_order_list_prompt_limits_domain():
    loaded = load_tasks(TASKS_DIR / "20261119T2229")

    assert "麦当劳" in loaded["s0-order-list"].script.prompt


def test_task_ids_match_file_names(tasks):
    for path in sorted(TASKS_DIR.glob("*.yaml")):
        assert tasks[path.stem].taskId == path.stem


def test_unknown_check_is_rejected_at_load(tasks):
    payload = tasks["order"].model_dump(mode="json")
    payload["expect"]["checks"] = ["not_a_check"]
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(payload)


def test_legacy_view_keeps_replay_fields(tasks):
    view = legacy_view(tasks["clarify-drink"])
    assert view["taskId"] == "clarify-drink"
    assert view["approve"] is True
    assert view["interactions"][0]["intentIds"] == ["request_meal_preference"]
    assert view["quoteItems"][0]["roundList"][0]["round"] == "3"
    assert view["account"] == "alice"


def test_spec_digest_is_stable(tasks):
    spec = tasks["order"]
    assert spec.digest() == spec.digest()
    assert len(spec.digest()) == 64


def old_spec(task, check):
    view = legacy_view(task)
    for field in ("expect", "approvals", "version"):
        view.pop(field)
    view["checks"] = [check]
    return _task_from_view(view)


def test_order_task_passes_with_matching_receipt(tasks):
    evidence = evidence_for(old_spec(tasks["order"], "reported_order"), f"已下单，订单号：{ORDER_ID}，待支付")
    verdict = judge(evidence, QUOTE, old_spec(tasks["order"], "reported_order"))
    assert verdict["status"] == "passed", verdict["checks"]


def test_order_task_fails_when_claiming_no_order(tasks):
    evidence = evidence_for(old_spec(tasks["order"], "reported_order"), "未下单，没有创建订单")
    verdict = judge(evidence, QUOTE, old_spec(tasks["order"], "reported_order"))
    assert verdict["status"] == "failed"
    assert verdict["checks"]["reportedOrder"] is False


def test_deny_task_passes_when_denial_is_reported(tasks):
    evidence = evidence_for(old_spec(tasks["deny"], "reported_denial"), "已拒绝本次下单，未执行下单", approvals=[approved_row(False)])
    verdict = judge(evidence, QUOTE, old_spec(tasks["deny"], "reported_denial"))
    assert verdict["status"] == "passed", verdict["checks"]


def test_quote_task_fails_when_claiming_order(tasks):
    evidence = evidence_for(old_spec(tasks["quote"], "no_claim_of_order"), f"已下单，订单号：{ORDER_ID}")
    verdict = judge(evidence, QUOTE, old_spec(tasks["quote"], "no_claim_of_order"))
    assert verdict["status"] == "failed"
    assert verdict["checks"]["noClaimOfOrder"] is False


def test_budget_check_uses_spec_budget(tasks):
    evidence = evidence_for(old_spec(tasks["budget-exceeded"], "budget_exceeded"), "总价 53.9 元，超过预算，未下单")
    verdict = judge(evidence, QUOTE, old_spec(tasks["budget-exceeded"], "budget_exceeded"))
    assert verdict["checks"]["quoteExceedsBudget"] is True


def test_reported_order_is_skipped_when_not_approved(tasks):
    spec = old_spec(tasks["budget-exceeded"], "budget_exceeded").model_copy(deep=True)
    spec.expect.checks = ["reported_order"]
    evidence = evidence_for(spec, "总价 53.9 元，超过预算，未下单")
    verdict = judge(evidence, QUOTE, spec)
    assert "reportedOrder" not in verdict["checks"]
    assert verdict["status"] == "passed"


def test_unavailable_when_evidence_incomplete(tasks):
    evidence = evidence_for(old_spec(tasks["order"], "reported_order"), f"已下单，订单号：{ORDER_ID}，待支付")
    evidence["status"] = "execution_error"
    assert judge(evidence, QUOTE, old_spec(tasks["order"], "reported_order"))["status"] == "unavailable"


def test_check_registry_matches_task_spec_ids(tasks):
    used = {check for spec in tasks.values() for check in spec.expect.checks}
    assert used <= set(CHECK_IDS)


@pytest.mark.parametrize("field, value", [("orderId", "missing"), ("storeCode", "addresses")])
def test_invalid_call_binding_is_rejected_at_load(tasks, field, value):
    payload = tasks["order"].model_dump(mode="json", by_alias=True)
    payload["expect"]["calls"][-1]["bind"] = {field: value}
    with pytest.raises(ValidationError, match="invalid binding"):
        TaskSpec.model_validate(payload)


def test_compatibility_view_round_trips_current_spec(tasks):
    for task in tasks.values():
        assert _task_from_view(legacy_view(task)).digest() == task.digest()
