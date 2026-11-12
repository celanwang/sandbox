import json

import httpx
import pytest

from conftest import ADMIN, MCP_TOKEN, ORDER, tool
from runner.__main__ import save
from runner.clients import Iris, Sandbox
from runner.episode import run_episode
from runner.scenarios import SCENARIOS


class FakeIris:
    def __init__(self, sandbox_client, name, *, route_to_sandbox=True, trace_available=True, order=ORDER):
        self.sandbox = sandbox_client
        self.name = name
        self.route_to_sandbox = route_to_sandbox
        self.trace_available = trace_available
        self.order = order
        self.confirmations = []
        self.pending = {}

    def handle(self, request):
        path = request.url.path
        if path == "/api/chat":
            data = json.loads(request.content)
            user_id = data["userId"]
            if "-preflight-" in user_id:
                if self.route_to_sandbox:
                    tool(self.sandbox, "delivery-query-addresses").raise_for_status()
                return httpx.Response(200, text="有一个公司地址。")
            tool(self.sandbox, "calculate-price", ORDER).raise_for_status()
            if self.name != "quote":
                self.pending[user_id] = {
                    "taskId": "task",
                    "content": {"approvals": {"iris-call-id": {
                        "id": "approval", "tool": "create-order", "arguments": self.order, "status": "PENDING",
                    }}},
                }
            return httpx.Response(200, text="总价 53.90 元。")
        if path == "/api/agent/state":
            task = self.pending.get(request.url.params["userId"])
            return httpx.Response(200, json={"tasks": [task] if task else []})
        if path == "/api/agent/confirm":
            data = json.loads(request.content)
            self.confirmations.append(data)
            del self.pending[data["userId"]]
            if data["approved"]:
                result = tool(self.sandbox, "create-order", ORDER)
                if result.status_code == 504:
                    tool(self.sandbox, "order-list").raise_for_status()
                    return httpx.Response(200, json={"reply": "提交超时，目前无法确认下单结果。"})
            return httpx.Response(200, json={"reply": "审批已处理。"})
        if path == "/api/trace":
            if not self.trace_available:
                return httpx.Response(503)
            return httpx.Response(200, json=[{"type": "TOOL_RESULT", "id": "iris-event-id"}])
        raise AssertionError(path)


@pytest.fixture
def sandbox_client(client):
    client.base_url = "http://localhost/admin"
    client.headers.update(ADMIN)
    return Sandbox(client, MCP_TOKEN)


@pytest.mark.parametrize("name", SCENARIOS)
def test_five_scripted_cases_and_evidence(client, sandbox_client, tmp_path, name):
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
