import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import ADMIN, MCP_TOKEN, ORDER, admin, rpc, start, tool


def body(response):
    return json.loads(response.raise_for_status().json()["result"]["content"][0]["text"])


def test_engineering_fixture_records_address_and_coupon_changes(client):
    episode = start(client, scene="engineering")
    names = {tool["name"] for tool in rpc(client, "tools/list", {}).json()["result"]["tools"]}
    assert {"delivery-update-address", "available-coupons", "auto-bind-coupons"} <= names
    updated = {"addressId": "addr_001", "address": "深圳市南山区示例路 18 号 B 座前台", "phone": "13800000002"}
    assert body(tool(client, "delivery-update-address", updated))["success"]
    address, = body(tool(client, "delivery-query-addresses"))["data"]["addresses"]
    assert address["fullAddress"] == updated["address"]
    assert address["phone"] == "138****0002"
    assert body(tool(client, "available-coupons"))["data"][0]["couponId"] == "Coupon001"
    tool(client, "auto-bind-coupons").raise_for_status()
    assert body(tool(client, "available-coupons"))["data"] == []
    tool(client, "auto-bind-coupons").raise_for_status()
    snapshot = admin(client, episode, "snapshot").json()
    assert snapshot["state"]["addresses"]["addr_001"]["phone"] == updated["phone"]
    assert list(snapshot["state"]["coupons"]) == ["Coupon001"]
    assert snapshot["state"]["claimableCoupons"] == {}
    assert snapshot["calls"][-1]["business"]["success"] is False
    assert snapshot["calls"][-1]["changes"] == []
    assert {change["collection"] for change in snapshot["calls"][3]["changes"]} == {"coupons", "claimableCoupons"}


def test_engineering_order_requires_matching_quote_and_supports_cancel_then_reorder(client):
    episode = start(client, scene="engineering")
    params = {"addressId": "addr_001", "storeCode": "mock_store_001", "beCode": "mock_delivery_001",
              "beType": 2, "orderType": 2, "items": [{"productCode": "meal_001", "quantity": 1}]}
    assert not body(tool(client, "create-order", params))["success"]
    assert body(tool(client, "calculate-price", params))["data"]["price"] == 3500
    first = body(tool(client, "create-order", params))["data"]["orderId"]
    assert body(tool(client, "cancel-order", {"orderId": first, "cancelReasonCode": 1}))["data"]["cancelResult"]
    params["items"] = [{"productCode": "meal_001", "quantity": 2}]
    assert not body(tool(client, "create-order", params))["success"]
    assert body(tool(client, "calculate-price", params))["data"]["price"] == 6400
    second = body(tool(client, "create-order", params))["data"]["orderId"]
    orders = admin(client, episode, "snapshot").json()["state"]["orders"]
    assert first != second
    assert orders[first]["status"] == "已取消"
    assert orders[second]["status"] == "待支付"
    assert orders[second]["amounts"]["totalFen"] == 6400


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
    assert tool(client, "create-order", ORDER).status_code == 401
    assert tool(client, "create-order", ORDER, endpoint=f"/mcp/{run_id}/2").status_code == result.status_code


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
    assert body(tool(client, "create-order", ORDER, endpoint=f"/mcp/{run_id}/2"))["success"] is False


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
    assert client.post("/admin/episodes", headers=ADMIN, json={"token": MCP_TOKEN}).status_code == 409
    third = start(client, token="third-mcd")
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
        assert len(list(tmp_path.glob("*.json"))) == 1
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


def test_episode_open_is_idempotent_and_old_credentials_cannot_write(client):
    from uuid import uuid4

    episode_id = uuid4().hex
    payload = {"episodeId": episode_id, "token": MCP_TOKEN}
    assert client.post("/admin/episodes", headers=ADMIN, json=payload).status_code == 201
    retry = client.post("/admin/episodes", headers=ADMIN, json=payload)
    assert retry.status_code == 200 and retry.json()["episodeId"] == episode_id
    assert client.post("/admin/episodes", headers=ADMIN, json={**payload, "seed": 2}).status_code == 409
    admin(client, episode_id, "close", "POST")
    assert client.post("/admin/episodes", headers=ADMIN, json=payload).status_code == 200
    assert client.post("/admin/episodes", headers=ADMIN, json={"token": MCP_TOKEN}).status_code == 409

    current = start(client, token="current")
    old_url = f"/mcp/{episode_id}/1"
    assert tool(client, "create-order", ORDER, token="current", endpoint=old_url).status_code == 401
    assert tool(client, "create-order", ORDER).status_code == 401
    wrong_meta = tool(client, "create-order", ORDER, token="current", meta={"runId": episode_id})
    assert wrong_meta.raise_for_status().json()["result"]["isError"] is True
    assert not admin(client, current, "snapshot").json()["state"]["orders"]


def test_old_generation_cannot_issue_requests_after_reset(client):
    episode = start(client)
    assert tool(client, "order-list", endpoint=f"/mcp/{episode}/1").status_code == 200
    admin(client, episode, "reset", "POST")
    assert tool(client, "create-order", ORDER, endpoint=f"/mcp/{episode}/1").status_code == 401
    assert client.post(f"/admin/episodes/{episode}/round", headers=ADMIN,
                       json={"generation": 1, "round": 1}).status_code == 409
    assert client.post(f"/admin/episodes/{episode}/close?generation=1", headers=ADMIN).status_code == 409
    assert tool(client, "order-list", endpoint=f"/mcp/{episode}/2").status_code == 200
    assert not admin(client, episode, "snapshot").json()["state"]["orders"]


def test_retired_token_remains_retired_after_archived_episode_is_deleted(tmp_path):
    from starlette.testclient import TestClient

    from sandbox.server import create_app

    with TestClient(create_app(admin_token="test-admin", archive_dir=tmp_path), base_url="http://localhost") as http:
        episode = start(http)
        admin(http, episode, "close", "POST")
        assert http.delete(f"/admin/episodes/{episode}", headers=ADMIN).status_code == 204
    with TestClient(create_app(admin_token="test-admin", archive_dir=tmp_path), base_url="http://localhost") as http:
        assert http.post("/admin/episodes", headers=ADMIN, json={"token": MCP_TOKEN}).status_code == 409


def test_reservation_is_consistent_in_order_receipts(client):
    start(client)
    slot = "2026-11-10 13:00"
    created = body(tool(client, "create-order", {**ORDER, "reservationDate": slot}))["data"]
    queried = body(tool(client, "query-order", {"orderId": created["orderId"]}))["data"]
    for receipt in (created["orderDetail"], queried):
        assert receipt["deliveryInfo"]["deliveryType"] == "预约配送"
        assert receipt["deliveryInfo"]["expectDeliveryTime"] == slot
