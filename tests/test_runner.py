import json

import httpx
import pytest

from conftest import ADMIN, MCP_TOKEN, ORDER, tool
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
            result = tool(self.sandbox, "calculate-price", ORDER).raise_for_status().json()
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
                result = tool(self.sandbox, "create-order", ORDER)
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


@pytest.mark.parametrize("name", SCENARIOS)
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
    assert evidence["status"] == "capture_error"
    assert evidence["verdict"]["status"] == "unavailable"
    assert "HTTP 503" in evidence["errors"]["iris.trace"]
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


def test_interrupt_releases_token(sandbox_client):
    class InterruptedIris:
        def chat(self, identity, prompt):
            raise KeyboardInterrupt

    evidence = run_episode("order", InterruptedIris(), sandbox_client)
    assert evidence["status"] == "interrupted"
    assert evidence["sandbox"]["closed"] is True
    fresh_id = sandbox_client.start(SCENARIOS["order"], 0)
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


def test_interrupt_during_final_trace_releases_token(client, sandbox_client):
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
    assert sandbox_client.start(SCENARIOS["order"], 0) != evidence["runId"]
