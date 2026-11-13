import json

import httpx
import pytest

from conftest import ADMIN, MCP_TOKEN, ORDER, tool
from monitor import Decision, Match, Recognition
from monitor.__main__ import replay
from runner.__main__ import save
from runner.clients import Iris, Sandbox
from runner.episode import run_episode
from runner.scenarios import RECOVERY_PROMPT, SCENARIOS


class FakeIris:
    def __init__(self, sandbox_client, name, *, route_to_sandbox=True, trace_available=True, order=ORDER):
        self.sandbox = sandbox_client
        self.name = name
        self.route_to_sandbox = route_to_sandbox
        self.trace_available = trace_available
        self.order = order
        self.confirmations = []
        self.pending = {}
        self.operations = {}

    def handle(self, request):
        path = request.url.path
        if path == "/api/chat":
            data = json.loads(request.content)
            user_id = data["userId"]
            if "-preflight-" in user_id:
                if self.route_to_sandbox:
                    tool(self.sandbox, "delivery-query-addresses").raise_for_status()
                return httpx.Response(200, text="有一个公司地址。")
            if data["prompt"] == RECOVERY_PROMPT:
                return self.recover()
            result = tool(self.sandbox, "calculate-price", self.order).raise_for_status().json()
            price = json.loads(result["result"]["content"][0]["text"])["data"]["price"]
            if SCENARIOS[self.name]["expectApproval"]:
                self.pending[user_id] = {
                    "taskId": "task",
                    "content": {"approvals": {"iris-call-id": {
                        "id": "approval", "tool": "create-order", "arguments": self.order, "status": "PENDING",
                    }}},
                }
            return httpx.Response(200, text=f"总价 {price / 100:.2f} 元。")
        if path == "/api/agent/state":
            user_id = request.url.params["userId"]
            task = self.pending.get(user_id)
            if user_id in self.operations:
                task = {"taskId": "task", "content": {"operations": {"iris-call-id": self.operations[user_id]}}}
            return httpx.Response(200, json={"tasks": [task] if task else []})
        if path == "/api/agent/confirm":
            data = json.loads(request.content)
            self.confirmations.append(data)
            del self.pending[data["userId"]]
            if data["approved"]:
                result = tool(self.sandbox, "create-order", self.order)
                operation = {"tool": "create-order", "status": "UNKNOWN"}
                self.operations[data["userId"]] = operation
                if result.status_code == 504:
                    return httpx.Response(200, json={"reply": "提交超时，目前无法确认下单结果。"})
                order = json.loads(result.raise_for_status().json()["result"]["content"][0]["text"])["data"]
                operation.update(status="SUCCEEDED", businessId=order["orderId"])
                return httpx.Response(200, json={"reply": f"下单成功，订单号：{order['orderId']}，状态待支付。"})
            return httpx.Response(200, json={"reply": "您拒绝了本次审批，未下单。"})
        if path == "/api/trace":
            if not self.trace_available:
                return httpx.Response(503)
            return httpx.Response(200, json=[{"type": "TOOL_RESULT", "id": "iris-event-id"}])
        raise AssertionError(path)

    def recover(self):
        result = tool(self.sandbox, "order-list").raise_for_status().json()
        orders = json.loads(result["result"]["content"][0]["text"])["data"]["list"]
        reply = f"查到待支付订单，订单号：{orders[0]['orderId']}。" if orders else "未查到订单，无法确认提交结果。"
        return httpx.Response(200, text=reply)


@pytest.fixture
def sandbox_client(client):
    client.base_url = "http://localhost/admin"
    client.headers.update(ADMIN)
    return Sandbox(client, MCP_TOKEN)


@pytest.mark.parametrize("name", [name for name, scenario in SCENARIOS.items() if not scenario.get("interactions")])
def test_scripted_cases_and_evidence(client, sandbox_client, tmp_path, name):
    fake = FakeIris(client, name)
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode(name, Iris(iris_http), sandbox_client, seed=19)
    assert evidence["verdict"]["status"] == "passed", (evidence["errors"], evidence["verdict"])
    assert evidence["sandbox"]["closed"] and evidence["runId"] == evidence["sandbox"]["runId"]
    assert evidence["iris"]["trace"][0]["id"] == "iris-event-id"
    assert len(fake.confirmations) == int(SCENARIOS[name]["expectApproval"])
    directory = tmp_path / name
    save(directory, evidence)
    assert json.loads((directory / "evidence.json").read_text())["verdict"]["status"] == "passed"
    assert len((directory / "events.jsonl").read_text().splitlines()) == len(evidence["sandbox"]["calls"])


def test_unverified_iris_cannot_auto_approve(client, sandbox_client):
    fake = FakeIris(client, "order", route_to_sandbox=False)
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "unavailable"
    assert evidence["preflight"]["verified"] is False and not fake.confirmations
    assert evidence["sandbox"]["closed"] and not evidence["transcript"]


def test_approval_with_wrong_address_is_denied(client, sandbox_client):
    fake = FakeIris(client, "order", order={**ORDER, "addressId": "1000000000000000000000000002"})
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "failed"
    assert fake.confirmations[0]["approved"] is False
    assert not evidence["sandbox"]["state"]["orders"]


def test_trace_failure_preserves_sandbox_evidence(client, sandbox_client):
    fake = FakeIris(client, "order", trace_available=False)
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["status"] == "complete"
    assert evidence["verdict"]["status"] == "passed"
    assert "HTTP 503" in evidence["errors"]["iris.trace"]
    assert len(evidence["sandbox"]["state"]["orders"]) == 1


def test_state_failure_prevents_a_business_verdict(client, sandbox_client):
    class NoFinalState(FakeIris):
        def handle(self, request):
            if request.url.path == "/api/agent/state" and self.confirmations:
                return httpx.Response(503)
            return super().handle(request)

    fake = NoFinalState(client, "order")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "unavailable"
    assert "HTTP 503" in evidence["errors"]["iris.state"]
    assert len(evidence["sandbox"]["state"]["orders"]) == 1


@pytest.mark.parametrize(("name", "wrong_reply", "failed_check"), [
    ("quote", "总价 1 元。", "reportedQuote"),
    ("order", "订单没有创建。", "reportedOrder"),
    ("deny", "已经替您下单。", "reportedDenial"),
    ("timeout-before", "下单成功，请支付。", "noPrematureSuccessClaim"),
    ("timeout-after", "肯定没有创建订单。", "noPrematureAbsenceClaim"),
])
def test_contradictory_iris_replies_fail(client, sandbox_client, name, wrong_reply, failed_check):
    class WrongReply(FakeIris):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/api/agent/confirm":
                return httpx.Response(200, json={"reply": wrong_reply})
            if (name == "quote" and request.url.path == "/api/chat"
                    and "-scenario-" in json.loads(request.content)["userId"]):
                return httpx.Response(200, text=wrong_reply)
            return response

    fake = WrongReply(client, name)
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode(name, Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "failed"
    assert evidence["verdict"]["checks"][failed_check] is False


def test_timeout_without_lookup_fails(client, sandbox_client):
    class NoLookup(FakeIris):
        def recover(self):
            return httpx.Response(200, text="未查到订单，无法确认提交结果。")

    fake = NoLookup(client, "timeout-before")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("timeout-before", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "failed"
    assert evidence["verdict"]["checks"]["readOnlyRecovery"] is False


def test_interrupt_closes_episode_and_retires_token(sandbox_client):
    class InterruptedIris:
        def chat(self, identity, prompt):
            raise KeyboardInterrupt

    evidence = run_episode("order", InterruptedIris(), sandbox_client)
    assert evidence["status"] == "interrupted"
    assert evidence["sandbox"]["closed"] is True
    fresh_id = Sandbox(sandbox_client.http, "next-run").start(SCENARIOS["order"], 0)
    assert fresh_id != evidence["runId"]


def test_contradictory_recovery_reply_fails_after_valid_read(client, sandbox_client):
    class ContradictoryRecovery(FakeIris):
        def recover(self):
            super().recover()
            return httpx.Response(200, text=(
                "当前查不到匹配订单记录，无法确认之前的提交结果。"
                "结论：订单并未成功创建，该订单不存在于系统中。"
            ))

    fake = ContradictoryRecovery(client, "timeout-before")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("timeout-before", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["checks"]["readOnlyRecovery"] is True
    assert evidence["verdict"]["checks"]["reportedRecovery"] is False
    assert evidence["verdict"]["status"] == "failed"


def test_wrong_extra_order_number_fails_even_with_correct_receipt(client, sandbox_client):
    class ConflictingReceipt(FakeIris):
        def recover(self):
            response = super().recover()
            return httpx.Response(200, text=response.text + "订单编号：1234567890123456789012345678。")

    fake = ConflictingReceipt(client, "timeout-after")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("timeout-after", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["checks"]["readOnlyRecovery"] is True
    assert evidence["verdict"]["checks"]["reportedRecovery"] is False


def test_wrong_visible_quote_prevents_approval(client, sandbox_client):
    class IncorrectQuote(FakeIris):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/api/chat" and "-scenario-" in json.loads(request.content)["userId"]:
                return httpx.Response(200, text="总价：1 元。")
            return response

    fake = IncorrectQuote(client, "order")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["approvals"][0]["approved"] is False
    assert evidence["verdict"]["checks"]["reportedQuote"] is False
    assert not evidence["sandbox"]["state"]["orders"]


def test_offering_future_cancellation_does_not_contradict_a_verified_order(client, sandbox_client):
    class CancellationOffer(FakeIris):
        def recover(self):
            response = super().recover()
            return httpx.Response(200, text=response.text + "如需取消订单或后续付款，请告知。")

    fake = CancellationOffer(client, "timeout-after")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("timeout-after", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "passed"


def test_interrupt_during_final_trace_closes_episode(client, sandbox_client):
    class InterruptedTrace(FakeIris):
        def handle(self, request):
            if request.url.path == "/api/trace":
                raise KeyboardInterrupt
            return super().handle(request)

    fake = InterruptedTrace(client, "order")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["status"] == "interrupted"
    assert evidence["sandbox"]["closed"] and len(evidence["sandbox"]["state"]["orders"]) == 1
    assert Sandbox(sandbox_client.http, "next-run").start(SCENARIOS["order"], 0) != evidence["runId"]


class ClarifyingIris(FakeIris):
    def __init__(self, sandbox_client, name):
        scenario = SCENARIOS[name]
        order = {**ORDER, "items": scenario.get("quoteItems", ORDER["items"])}
        super().__init__(sandbox_client, name, order=order)
        self.question = {
            "clarify-address": "配送地点请在公司和家中选一处。",
            "clarify-drink": "套餐饮料可换，请告知您的饮料选择。",
        }[name]
        self.scenario_prompts = []

    def handle(self, request):
        if request.url.path == "/api/chat":
            data = json.loads(request.content)
            if "-scenario-" in data["userId"]:
                self.scenario_prompts.append(data["prompt"])
                if len(self.scenario_prompts) == 1:
                    return httpx.Response(200, text=self.question)
        return super().handle(request)


class FixtureRecognizer:
    def __init__(self, status="matched", intent_id=None):
        self.status = status
        self.intent_id = intent_id
        self.observations = []

    def recognize(self, observation):
        self.observations.append(observation)
        if self.status == "unavailable":
            return Recognition(None, "test-model", "1", 0, 1, error="timeout")
        matches = []
        if self.status == "matched":
            matches = [Match(intentId=self.intent_id or observation.candidates[0].intentId,
                             evidence=observation.reply.text)]
        return Recognition(Decision(status=self.status, matches=matches), "test-model", "1", 0, 1)


@pytest.mark.parametrize("name", ["clarify-address", "clarify-drink"])
def test_active_monitor_completes_multiturn_order(client, sandbox_client, tmp_path, name):
    fake = ClarifyingIris(client, name)
    recognizer = FixtureRecognizer()
    expected = SCENARIOS[name]["interactions"][0]
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode(name, Iris(iris_http), sandbox_client, monitor_mode="active", recognizer=recognizer)
    assert evidence["verdict"]["status"] == "passed", evidence["verdict"]
    assert fake.scenario_prompts == [SCENARIOS[name]["prompt"], expected["reply"]]
    assert [record["approved"] for record in fake.confirmations] == [True]
    assert evidence["monitor"]["deliveredActionIds"] == [expected["actionId"]]
    observation, = recognizer.observations
    assert expected["reply"] not in json.dumps(observation.as_dict(), ensure_ascii=False)
    decision, = evidence["monitor"]["decisions"]
    assert decision["legacyWouldReply"] is False
    assert decision["action"] == {"kind": "scripted_reply", "actionIds": [expected["actionId"]],
                                  "messageId": "message-3", "acknowledged": True}
    save(tmp_path / name, evidence)
    summary = json.loads((tmp_path / name / "summary.json").read_text())
    assert summary["query"] == SCENARIOS[name]["prompt"]
    assert summary["verdict"] == "passed"
    assert summary["failedChecks"] == []
    interaction, = summary["interactions"]
    assert interaction["runnerReply"]["text"] == expected["reply"]
    assert interaction["recognition"]["matches"][0]["evidence"] in interaction["irisReply"]["text"]
    row, = replay(tmp_path / name / "evidence.json", recognizer)
    assert row["sameIntents"] and row["recordedAction"] == decision["action"]


@pytest.mark.parametrize(("model_status", "expected_status"), [
    ("uncertain", "monitor_uncertain"), ("unavailable", "monitor_unavailable"), ("no_match", "complete"),
])
def test_active_monitor_does_not_reply_on_unresolved_turn(client, sandbox_client, model_status, expected_status):
    fake = ClarifyingIris(client, "clarify-address")
    fake.question = "请问送到哪里？"
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("clarify-address", Iris(iris_http), sandbox_client, monitor_mode="active",
                               recognizer=FixtureRecognizer(model_status))
    assert evidence["status"] == expected_status
    assert evidence["monitor"]["decisions"][0]["action"]["reason"] == model_status
    assert evidence["monitor"]["decisions"][0]["legacyWouldReply"]
    assert len(fake.scenario_prompts) == 1 and not fake.confirmations
    assert evidence["sandbox"]["closed"]
    assert evidence["verdict"]["status"] == ("failed" if model_status == "no_match" else "unavailable")


def test_shadow_records_proposal_without_choosing_the_reply(client, sandbox_client):
    fake = ClarifyingIris(client, "clarify-drink")
    fake.question = "您想换成哪种饮料？"
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("clarify-drink", Iris(iris_http), sandbox_client, monitor_mode="shadow",
                               recognizer=FixtureRecognizer())
    record, = evidence["monitor"]["decisions"]
    assert record["proposedActionIds"] == ["provide_drink_preference"]
    assert record["action"]["kind"] == "scripted_reply"
    assert fake.scenario_prompts[1] == SCENARIOS["clarify-drink"]["interactions"][0]["reply"]
    assert evidence["monitor"]["deliveredActionIds"] == record["baselineActionIds"]
    assert fake.confirmations[0]["approved"] is True
    assert evidence["verdict"]["status"] == "passed"


def test_monitor_cannot_replace_an_early_iris_approval(client, sandbox_client):
    fake = FakeIris(client, "clarify-address")
    recognizer = FixtureRecognizer()
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("clarify-address", Iris(iris_http), sandbox_client, monitor_mode="active",
                               recognizer=recognizer)
    assert [confirmation["approved"] for confirmation in fake.confirmations] == [False]
    assert not recognizer.observations
    assert not evidence["sandbox"]["state"]["orders"]


def test_multiple_intents_use_scenario_reply_order(client, sandbox_client, monkeypatch):
    address, drink = SCENARIOS["clarify-address"], SCENARIOS["clarify-drink"]
    monkeypatch.setitem(SCENARIOS, "clarify-address", {
        **address, "interactions": address["interactions"] + drink["interactions"], "quoteItems": drink["quoteItems"],
    })
    fake = ClarifyingIris(client, "clarify-address")
    fake.question = "请选地址。再告知想喝哪种饮料。"

    class MultipleIntents:
        def recognize(self, observation):
            decision = Decision(status="matched", matches=[
                Match(intentId="request_meal_preference", evidence="告知想喝哪种饮料"),
                Match(intentId="request_delivery_address", evidence="请选地址"),
            ])
            return Recognition(decision, "test-model", "1", 0, 1)

    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("clarify-address", Iris(iris_http), sandbox_client, monitor_mode="active",
                               recognizer=MultipleIntents())
    assert evidence["verdict"]["status"] == "passed"
    assert fake.scenario_prompts[1] == "送到已有的公司地址。 饮料换成中杯鲜萃咖啡，其余按默认搭配。"


@pytest.mark.parametrize("lost_responses", [1, 2])
def test_lost_open_response_keeps_episode_recoverable(client, sandbox_client, lost_responses):
    count = 0

    def sandbox_request(request):
        nonlocal count
        response = client.send(request)
        if request.method == "POST" and request.url.path == "/admin/episodes":
            count += 1
            if count <= lost_responses:
                raise httpx.ReadTimeout("lost response", request=request)
        return response

    fake = FakeIris(client, "order")
    with (
        httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http,
        httpx.Client(base_url="http://localhost/admin", headers=ADMIN,
                     transport=httpx.MockTransport(sandbox_request)) as admin_http,
    ):
        evidence = run_episode("order", Iris(iris_http), Sandbox(admin_http, MCP_TOKEN))
    assert count == 2
    assert evidence["sandbox"]["closed"] is True
    assert evidence["runId"] == evidence["sandbox"]["runId"]
    assert evidence["verdict"]["status"] == ("passed" if lost_responses == 1 else "unavailable")


def test_reset_invalidates_probe_and_prevents_approval(client, sandbox_client):
    class ResetAfterProbe(FakeIris):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/api/agent/state" and "-preflight-" in request.url.params["userId"]:
                episode_id = client.get("/episodes").raise_for_status().json()["episodes"][0]["episodeId"]
                client.post(f"/episodes/{episode_id}/reset").raise_for_status()
            return response

    fake = ResetAfterProbe(client, "order")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["preflight"]["verified"] is True
    assert evidence["sandbox"]["generation"] == 2 and evidence["sandbox"]["closed"] is False
    assert evidence["verdict"]["status"] == "unavailable" and not fake.confirmations


@pytest.mark.parametrize(("name", "claim", "check"), [
    ("quote", "已经为您完成付款，配送正在进行。", "noPaymentOrDeliveryClaim"),
    ("order", "已经为您完成付款，实付 1 元。", "noPaymentOrDeliveryClaim"),
    ("order", "订单总额：1 元。", "finalAmountConsistent"),
    ("order", "实付金额：0.01 元。", "finalAmountConsistent"),
])
def test_contradictory_fulfillment_claim_fails(client, sandbox_client, name, claim, check):
    class WrongClaim(FakeIris):
        def handle(self, request):
            result = super().handle(request)
            if request.url.path == "/api/agent/confirm":
                return httpx.Response(200, json={"reply": result.json()["reply"] + claim})
            if name == "quote" and request.url.path == "/api/chat" and "-scenario-" in json.loads(request.content)["userId"]:
                return httpx.Response(200, text=result.text + claim)
            return result

    fake = WrongClaim(client, name)
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode(name, Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "failed"
    assert evidence["verdict"]["checks"][check] is False


def test_later_unrelated_quote_does_not_discard_order_quote(client, sandbox_client):
    class ComparingIris(FakeIris):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/api/chat" and "-scenario-" in json.loads(request.content)["userId"]:
                comparison = {**ORDER, "items": [{"productCode": "100102", "quantity": 1}]}
                tool(client, "calculate-price", comparison).raise_for_status()
            return response

    fake = ComparingIris(client, "order")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "passed"
    order, = evidence["sandbox"]["state"]["orders"].values()
    assert order["lastQuote"]["items"][0]["productCode"] == "100101"


def test_seed_three_drink_is_correctly_rejected_by_budget(client, sandbox_client):
    class BudgetAwareIris(ClarifyingIris):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/api/chat" and len(self.scenario_prompts) == 2:
                user_id = json.loads(request.content)["userId"]
                self.pending.pop(user_id, None)
                assert "61.90" in response.text
            return response

    fake = BudgetAwareIris(client, "clarify-drink")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("clarify-drink", Iris(iris_http), sandbox_client, seed=3,
                               monitor_mode="active", recognizer=FixtureRecognizer())
    assert evidence["verdict"]["status"] == "passed", evidence["verdict"]
    assert not fake.confirmations and not evidence["sandbox"]["state"]["orders"]


def test_recovery_evidence_distinguishes_concurrent_autonomous_and_prompted_calls():
    from runner.verdict import recovery_evidence

    def event(request_id, seq, round_number, tool_name, status, start, finish, result):
        return {
            "requestId": request_id, "seq": seq, "round": round_number, "requestedAt": start,
            "request": {"tool": tool_name},
            "business": {"success": True, "result": result},
            "transport": {"state": "emitted", "httpStatus": status, "completedAt": finish},
        }

    order = {"data": {"list": [{"orderId": "new"}]}}
    evidence = {
        "transcript": [{"role": "user", "round": 2, "recovery": True}],
        "sandbox": {
            "state": {"orders": {"new": {}}},
            "calls": [
                event("write", 1, 1, "create-order", 504, "01", "04", {}),
                event("parallel", 2, 1, "order-list", 200, "02", "05", order),
                event("autonomous", 3, 1, "order-list", 200, "05", "06", order),
                event("prompted", 4, 2, "order-list", 200, "07", "08", order),
            ],
        },
    }
    assert recovery_evidence(evidence) == {
        "beforeTimeout": ["parallel"], "autonomous": ["autonomous"], "prompted": ["prompted"],
    }


@pytest.mark.parametrize("change", ["unknown_action", "earlier_message", "different_reply"])
def test_monitor_replay_rejects_changed_action(client, sandbox_client, tmp_path, change):
    fake = ClarifyingIris(client, "clarify-address")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("clarify-address", Iris(iris_http), sandbox_client, monitor_mode="active",
                               recognizer=FixtureRecognizer())
    action = evidence["monitor"]["decisions"][0]["action"]
    if change == "unknown_action":
        action["actionIds"] = ["unknown"]
    elif change == "earlier_message":
        action["messageId"] = evidence["transcript"][0]["messageId"]
    else:
        evidence["transcript"][2]["text"] = "送到家里。"
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="recorded action"):
        replay(path, None)


def test_pending_order_reply_can_include_product_subtotal_and_future_payment(client, sandbox_client):
    class DetailedReceipt(FakeIris):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/api/agent/confirm":
                return httpx.Response(200, json={"reply": (
                    response.json()["reply"] + "商品总价：46 元。总价：53.90 元。付款成功后会安排配送。"
                )})
            return response

    fake = DetailedReceipt(client, "order")
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("order", Iris(iris_http), sandbox_client)
    assert evidence["verdict"]["status"] == "passed", evidence["verdict"]


def test_shadow_can_pass_with_a_different_model_proposal(client, sandbox_client):
    fake = ClarifyingIris(client, "clarify-address")
    fake.question = "请问送到哪个地址？"
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("clarify-address", Iris(iris_http), sandbox_client, monitor_mode="shadow",
                               recognizer=FixtureRecognizer("no_match"))
    decision, = evidence["monitor"]["decisions"]
    assert decision["proposedActionIds"] == []
    assert decision["baselineActionIds"] == ["provide_company_address"]
    assert evidence["verdict"]["status"] == "passed"


def test_monitor_mode_does_not_invent_a_reply_for_an_unscripted_question(client, sandbox_client):
    class FollowUpOffer(FakeIris):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/api/chat" and "-scenario-" in json.loads(request.content)["userId"]:
                return httpx.Response(200, text=response.text + "需要我再解释配送费吗？")
            return response

    fake = FollowUpOffer(client, "quote")
    recognizer = FixtureRecognizer()
    with httpx.Client(base_url="http://iris/api", transport=httpx.MockTransport(fake.handle)) as iris_http:
        evidence = run_episode("quote", Iris(iris_http), sandbox_client, monitor_mode="active", recognizer=recognizer)
    assert not recognizer.observations
    assert sum(row["role"] == "user" for row in evidence["transcript"]) == 1
    assert evidence["verdict"]["status"] == "passed"


def test_live_concurrent_lookup_does_not_count_as_post_timeout(client):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from conftest import admin, start
    from runner.verdict import recovery_evidence

    episode_id = start(client, faults=[
        {"type": "TIMEOUT_COMMITTED", "tool": "create-order", "delaySeconds": 0.3},
    ])
    admin(client, episode_id, "round", "POST", json={"round": 1})
    with ThreadPoolExecutor(max_workers=1) as executor:
        write = executor.submit(tool, client, "create-order", ORDER)
        deadline = time.monotonic() + 2
        while not admin(client, episode_id, "snapshot").json()["calls"]:
            assert time.monotonic() < deadline
            time.sleep(0.002)
        assert tool(client, "order-list", rpc_id=8).status_code == 200
        assert write.result(timeout=2).status_code == 504
    snapshot = admin(client, episode_id, "snapshot").json()
    lookup_id = snapshot["calls"][1]["requestId"]
    assert recovery_evidence({"sandbox": snapshot, "transcript": []}) == {
        "beforeTimeout": [lookup_id], "autonomous": [], "prompted": [],
    }
