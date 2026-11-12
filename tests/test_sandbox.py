import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import ADMIN, MCP_TOKEN, ORDER, admin, rpc, start, tool


def body(response):
    return json.loads(response.raise_for_status().json()["result"]["content"][0]["text"])


def test_tool_contract_and_export(client):
    run_id = start(client)
    init = rpc(client, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
    })
    assert "tools" in init.raise_for_status().json()["result"]["capabilities"]
    definitions = rpc(client, "tools/list", {}).raise_for_status().json()["result"]["tools"]
    assert len(definitions) == 13
    address = body(tool(client, "delivery-query-addresses"))["data"]["addresses"][0]
    assert address["addressId"] == ORDER["addressId"]
    stores = body(tool(client, "delivery-query-stores", {"addressId": address["addressId"], "beType": 2}))
    assert stores["data"][0]["storeCode"] == ORDER["storeCode"]
    menu = body(tool(client, "query-meals", {key: ORDER[key] for key in ("storeCode", "orderType", "beType")}))
    assert menu["data"]["meals"]["100101"]["name"] == "巨无霸三件套"
    admin(client, run_id, "round", "POST", json={"round": 1})
    quote = body(tool(client, "calculate-price", ORDER))
    assert quote["data"]["price"] == 5390
    response = tool(client, "create-order", ORDER, meta={"callId": "upstream-call"})
    order_id = body(response)["data"]["orderId"]
    receipt = body(tool(client, "query-order", {"orderId": order_id}))
    assert receipt["data"]["orderStatus"] == "待支付"
    cancelled = body(tool(client, "cancel-order", {"orderId": order_id, "cancelReasonCode": "2"}))
    assert cancelled["data"]["cancelResult"] is True

    snapshot = admin(client, run_id, "snapshot").json()
    events = [json.loads(line) for line in admin(client, run_id, "events").text.splitlines()]
    assert events == snapshot["calls"]
    assert len({event["requestId"] for event in events}) == len(events)
    created, cancelled = events[4], events[6]
    assert created["runId"] == run_id and created["round"] == 1
    assert created["request"] == {"rpcId": 7, "tool": "create-order", "arguments": ORDER, "meta": {"callId": "upstream-call"}}
    assert created["transport"]["body"] == response.text
    assert created["changes"][0]["before"] is None
    assert created["changes"][0]["after"]["quoteMatchesOrder"] is True
    assert cancelled["changes"][0]["before"]["status"] == "待支付"
    assert cancelled["changes"][0]["after"]["status"] == "已取消"
    assert snapshot["initialState"]["orders"] == {}


@pytest.mark.parametrize(("fault", "committed"), [("TIMEOUT_COMMITTED", True), ("TIMEOUT_NOT_COMMITTED", False)])
@pytest.mark.parametrize("mode", ["http", "rpc-error"])
def test_timeout_evidence(client, fault, committed, mode):
    run_id = start(client, faults=[{"type": fault, "tool": "create-order", "mode": mode}])
    result = tool(client, "create-order", ORDER)
    assert result.status_code == (504 if mode == "http" else 200)
    if mode == "rpc-error":
        assert result.json()["id"] == 7
        assert "error" in result.json()
    snapshot = admin(client, run_id, "snapshot").json()
    (event,) = snapshot["calls"]
    assert bool(snapshot["state"]["orders"]) is committed
    assert event["business"]["executed"] is committed
    assert event["business"]["success"] is (True if committed else None)
    assert bool(event["changes"]) is committed
    assert event["transport"]["state"] == "emitted"
    assert event["transport"]["body"] == result.text
    assert "orderId" not in result.text
    assert event["faults"] == [0]
    assert len(body(tool(client, "order-list"))["data"]["list"]) == int(committed)
    reset = admin(client, run_id, "reset", "POST").json()
    assert reset["generation"] == 2 and not reset["calls"] and not reset["state"]["orders"]
    assert tool(client, "create-order", ORDER).status_code == result.status_code


@pytest.mark.parametrize(("fault", "target", "args", "expected"), [
    ("SOLD_OUT", "100101", ORDER, "已售罄"),
    ("STORE_CLOSED", "100001", ORDER, "不在营业时间"),
    ("COUPON_INVALID", "sandbox-lunch", {
        **ORDER, "items": [{"productCode": "100102", "quantity": 1, "couponId": "sandbox-lunch"}],
    }, "当前不可用"),
])
def test_business_faults(client, fault, target, args, expected):
    run_id = start(client, faults=[{"type": fault, "target": target, "tool": "create-order"}])
    result = body(tool(client, "create-order", args))
    assert result["success"] is False and expected in result["message"]
    snapshot = admin(client, run_id, "snapshot").json()
    assert not snapshot["state"]["orders"]
    assert snapshot["calls"][0]["changes"] and snapshot["calls"][0]["faults"] == [0]


def test_start_fault_reapplied_after_reset(client):
    run_id = start(client, faults=[{"type": "SOLD_OUT", "target": "100101", "at": "start"}])
    assert body(tool(client, "create-order", ORDER))["success"] is False
    reset = admin(client, run_id, "reset", "POST").json()
    assert reset["startFaults"] == [0]
    assert reset["initialState"]["soldOut"] == reset["state"]["soldOut"] == {"100101": True}
    assert body(tool(client, "create-order", ORDER))["success"] is False


def test_management_auth_and_token_isolation(client):
    assert client.get("/admin/catalog").status_code == 401
    assert tool(client, "create-order", ORDER).status_code == 401
    first = start(client)
    second = start(client, token="second-mcd")
    assert client.post("/admin/episodes", headers=ADMIN, json={"token": MCP_TOKEN}).status_code == 409
    assert client.post("/admin/episodes", headers=ADMIN, json={"token": "x", "scene": "../tools"}).status_code == 400
    assert tool(client, "order-list", Origin="https://invalid.example").status_code == 403
    assert body(tool(client, "create-order", ORDER))["success"] is True
    assert not admin(client, second, "snapshot").json()["calls"]
    assert admin(client, first, "close", "POST").json()["closed"] is True
    assert tool(client, "order-list").status_code == 401
    third = start(client)
    assert first != third
    assert not admin(client, third, "snapshot").json()["state"]["orders"]


def test_same_rpc_id_concurrent_calls_are_distinct(client):
    run_id = start(client, faults=[{"type": "TIMEOUT_NOT_COMMITTED", "tool": "create-order", "nth": 2}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: tool(client, "create-order", ORDER, rpc_id=10), range(2)))
    assert sorted(result.status_code for result in responses) == [200, 504]
    snapshot = admin(client, run_id, "snapshot").json()
    first, second = snapshot["calls"]
    assert first["requestId"] != second["requestId"]
    assert [first["nth"], second["nth"]] == [1, 2]
    assert [first["transport"]["httpStatus"], second["transport"]["httpStatus"]] == [200, 504]
    assert len(snapshot["state"]["orders"]) == 1


@pytest.mark.parametrize("quantity", [1.9, True, 0, "1.2"])
def test_invalid_quantities_cannot_create_order(client, quantity):
    run_id = start(client)
    result = body(tool(client, "create-order", {**ORDER, "items": [{"productCode": "100101", "quantity": quantity}]}))
    assert result["success"] is False
    assert not admin(client, run_id, "snapshot").json()["state"]["orders"]


def test_reset_and_round_change_wait_for_admitted_post():
    import asyncio

    import httpx

    from sandbox.server import create_app

    async def run():
        app = create_app(admin_token="test-admin")
        headers = {
            "Authorization": f"Bearer {MCP_TOKEN}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_body():
            entered.set()
            await release.wait()
            yield json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "delivery-query-addresses", "arguments": {}},
            }).encode()

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
                response = await http.post("/admin/episodes", headers=ADMIN, json={"token": MCP_TOKEN})
                episode = response.raise_for_status().json()["episodeId"]
                base = f"/admin/episodes/{episode}"
                request = asyncio.create_task(http.post("/mcp", headers=headers, content=delayed_body()))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    for action in ("reset", "close", "round"):
                        response = await http.post(f"{base}/{action}", headers=ADMIN, json={"round": 1})
                        assert response.status_code == 409
                finally:
                    release.set()
                    response = await asyncio.wait_for(request, 2)
                assert response.status_code == 200
                snapshot = (await http.get(f"{base}/snapshot", headers=ADMIN)).raise_for_status().json()
                assert snapshot["calls"][0]["generation"] == 1
                reset = (await http.post(f"{base}/reset", headers=ADMIN)).raise_for_status().json()
                assert reset["generation"] == 2 and reset["calls"] == []

    asyncio.run(run())


def test_disconnected_client_records_planned_status_without_response():
    import asyncio

    import httpx

    from sandbox.server import create_app

    async def run():
        disconnected = asyncio.Event()
        messages = []
        app = create_app(admin_token="test-admin")
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "create-order", "arguments": ORDER},
        }).encode()

        async def receive():
            nonlocal body
            if body:
                request, body = body, b""
                return {"type": "http.request", "body": request, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
                response = await http.post("/admin/episodes", headers=ADMIN, json={
                    "token": MCP_TOKEN, "faults": [{
                        "type": "TIMEOUT_COMMITTED", "tool": "create-order", "delaySeconds": 0.15,
                    }],
                })
                episode = response.raise_for_status().json()["episodeId"]
                base = f"/admin/episodes/{episode}"
                scope = {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "POST", "scheme": "http", "path": "/mcp", "query_string": b"",
                    "headers": [(b"host", b"localhost"), (b"authorization", f"Bearer {MCP_TOKEN}".encode()),
                                (b"accept", b"application/json, text/event-stream"),
                                (b"content-type", b"application/json")],
                }
                request = asyncio.create_task(app(scope, receive, send))
                try:
                    async with asyncio.timeout(1):
                        while True:
                            snapshot = (await http.get(f"{base}/snapshot", headers=ADMIN)).raise_for_status().json()
                            if snapshot["calls"]:
                                break
                            await asyncio.sleep(0)
                    assert snapshot["calls"][0]["transport"]["state"] == "pending"
                finally:
                    disconnected.set()
                    await asyncio.wait_for(request, 2)
                final = (await http.get(f"{base}/snapshot", headers=ADMIN)).raise_for_status().json()
                outcome = final["calls"][0]["transport"]
                assert outcome["state"] == "disconnected"
                assert outcome["plannedHttpStatus"] == 504
                assert outcome["httpStatus"] is None and not messages
                assert len(final["state"]["orders"]) == 1

    asyncio.run(run())


def test_closed_episode_can_be_listed_and_purged(client):
    run_id = start(client)
    path = f"/admin/episodes/{run_id}"
    assert client.get("/admin/episodes").status_code == 401
    assert client.delete(path, headers=ADMIN).status_code == 409
    admin(client, run_id, "close", "POST")
    rows = client.get("/admin/episodes", headers=ADMIN).raise_for_status().json()["episodes"]
    assert rows == [{"episodeId": run_id, "closed": True, "domain": "mcd", "scene": "lunch"}]
    assert client.delete(path, headers=ADMIN).status_code == 204
    assert client.get(f"{path}/snapshot", headers=ADMIN).status_code == 404


def test_order_list_preserves_creation_order_on_same_clock(client):
    start(client)
    orders = [body(tool(client, "create-order", ORDER))["data"]["orderId"] for _ in range(12)]
    listed = body(tool(client, "order-list"))["data"]["list"]
    assert [order["orderId"] for order in listed] == list(reversed(orders[-10:]))


@pytest.mark.parametrize("arguments", [
    {key: value for key, value in ORDER.items() if key != "orderType"},
    {**ORDER, "reservationDate": "2020-01-01 01:00"},
    {**ORDER, "items": [{**ORDER["items"][0], "modification": {"values": [{"code": "unknown"}]}}]},
])
def test_invalid_order_contract_does_not_mutate_state(client, arguments):
    run_id = start(client)
    assert body(tool(client, "create-order", arguments))["success"] is False
    assert not admin(client, run_id, "snapshot").json()["state"]["orders"]


def test_due_faults_skipped_by_earlier_fault_are_recorded(client):
    run_id = start(client, faults=[
        {"type": "TIMEOUT_NOT_COMMITTED", "tool": "create-order"},
        {"type": "CONNECTION_FAIL", "tool": "create-order"},
        {"type": "SOLD_OUT", "tool": "create-order", "target": "100101"},
    ])
    assert tool(client, "create-order", ORDER).status_code == 504
    event = admin(client, run_id, "snapshot").json()["calls"][0]
    assert event["faults"] == [0]
    assert event["skippedFaults"] == [
        {"index": 1, "reason": "transport_fault_selected"},
        {"index": 2, "reason": "business_not_attempted"},
    ]


def test_closed_snapshot_survives_new_app_and_can_be_purged(tmp_path):
    from starlette.testclient import TestClient

    from sandbox.server import create_app

    with TestClient(create_app(admin_token="test-admin", archive_dir=tmp_path), base_url="http://localhost") as client:
        episode = start(client)
        body(tool(client, "create-order", ORDER))
        expected = admin(client, episode, "close", "POST").json()
        assert expected["closed"] and len(expected["state"]["orders"]) == 1
        assert len(list(tmp_path.iterdir())) == 1
    with TestClient(create_app(admin_token="test-admin", archive_dir=tmp_path), base_url="http://localhost") as client:
        assert admin(client, episode, "snapshot").json() == expected
        assert admin(client, episode, "close", "POST").json() == expected
        assert [json.loads(line) for line in admin(client, episode, "events").text.splitlines()] == expected["calls"]
        assert client.delete(f"/admin/episodes/{episode}", headers=ADMIN).status_code == 204
        assert client.get(f"/admin/episodes/{episode}/snapshot", headers=ADMIN).status_code == 404


def test_archive_failure_preserves_episode_and_can_retry(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from sandbox.server import create_app

    def fail(path, payload):
        raise OSError("test archive write failure")

    with TestClient(create_app(admin_token="test-admin", archive_dir=tmp_path), base_url="http://localhost") as client:
        episode = start(client)
        with monkeypatch.context() as patch:
            patch.setattr("sandbox.episodes.write_atomic", fail)
            result = client.post(f"/admin/episodes/{episode}/close", headers=ADMIN)
            assert result.status_code == 503
        assert not admin(client, episode, "snapshot").json()["closed"]
        assert body(tool(client, "delivery-query-addresses"))["success"] is True
        assert admin(client, episode, "close", "POST").json()["closed"] is True


def test_order_accepts_available_future_reservation(client):
    start(client)
    result = body(tool(client, "create-order", {**ORDER, "reservationDate": "2026-11-10 13:00"}))
    assert result["success"] is True
