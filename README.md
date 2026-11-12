# Sandbox

A stateful simulation environment for testing workflows and capturing execution traces.

Sandbox runs simulated services independently of the application under test. The first adapter is a synthetic McDelivery service with 13 MCP tools for delivery addresses, stores, menus, coupons, quotes, and orders. The episode API owns isolation, virtual time, fault injection, and trace export. The optional runner drives Iris through its existing chat and approval APIs.

## Run the sandbox

Use Python 3.11 or newer from the repository root.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export SANDBOX_ADMIN_TOKEN="$(openssl rand -hex 24)"
export MCD_MCP_TOKEN="$(openssl rand -hex 24)"
python -m sandbox.server
```

The service listens on `127.0.0.1:8100`. Use the same environment variables in the terminals that make requests. The admin bearer token manages episodes. The MCP bearer token selects an active episode.

To open an episode and list its tools without Iris, run the following in another terminal.

```sh
curl -fsS http://127.0.0.1:8100/admin/episodes \
  -H "Authorization: Bearer $SANDBOX_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  --data "{\"token\":\"$MCD_MCP_TOKEN\",\"scene\":\"lunch\",\"seed\":0}"

curl -fsS http://127.0.0.1:8100/mcp \
  -H "Authorization: Bearer $MCD_MCP_TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Run workflows with Iris

Start a dedicated Iris test instance with its own PostgreSQL, Redis, and model configuration. Apply these settings to the Iris process before startup.

```sh
export MCD_MCP_URL=http://127.0.0.1:8100/mcp
export MCD_MCP_TOKEN='<same value used by the runner>'
export IRIS_AUTO_REVIEW=false
```

With Sandbox and Iris running, start the runner from the Sandbox repository. The runner opens each episode, asks Iris to query delivery addresses, and checks for that call in the new episode before sending the scenario prompt. It approves a matching create-order request at most once, after verifying the quote and budget.

```sh
python -m runner --iris http://127.0.0.1:8080/api
python -m runner quote order deny timeout-before timeout-after --seed 0
```

Each episode writes `evidence.json` and `events.jsonl` under `runs/<batch>/<scenario>/`. The runner returns a nonzero exit code if any scenario fails its checks or required evidence is unavailable. Each token has one active episode at a time. The runner closes its episode after collecting evidence. Manual sessions can use a separate token or close their episode before reusing the runner token.

## Manage episodes and export traces

All paths in this table use `SANDBOX_ADMIN_TOKEN` as a bearer token.

| Method and path | Behavior |
| --- | --- |
| `GET /admin/catalog` | List domains and scenes. |
| `POST /admin/episodes` | Open an episode with `token`, optional `domain`, `scene`, `seed`, and `faults`. Defaults are `mcd`, `lunch`, and seed zero. |
| `POST /admin/episodes/{id}/round` | Set the round using `{"round": 1}`. |
| `GET /admin/episodes/{id}/snapshot` | Read the fault plan, initial and current state, and call events. |
| `GET /admin/episodes/{id}/events` | Export tool events as newline-delimited JSON. |
| `POST /admin/episodes/{id}/reset` | Restore the initial state and fault plan, clear calls, and increment the generation. |
| `POST /admin/episodes/{id}/close` | Seal the episode, return its final snapshot, and release the bearer token. |

To simulate an order that committed before timing out, pass this fault plan when opening an episode.

```json
{
  "faults": [{"type": "TIMEOUT_COMMITTED", "tool": "create-order", "nth": 1}]
}
```

`TIMEOUT_COMMITTED` and `TIMEOUT_NOT_COMMITTED` both return HTTP 504. The former runs the tool before substituting the 504. `CONNECTION_FAIL` returns 502 and `AUTH_401` returns 401. Transport faults also accept `mode: "rpc-error"` to emit a JSON-RPC error under HTTP 200, and `delaySeconds` to delay the response. Business faults `SOLD_OUT`, `STORE_CLOSED`, and `COUPON_INVALID` take a product, store, or coupon ID in `target`. They can apply at episode start via `"at": "start"`. Faults can target a named tool or all calls via `"*"`, and `persist: true` repeats a fault from its configured call onward.

Each version 1 `tool.call` event includes the run ID, generation, request ID, JSON-RPC ID, arguments, optional MCP metadata, fault indexes, business result, entity changes, and the HTTP status and body emitted by the service. Entity changes identify the collection and ID and contain the before and after values. A committed timeout can therefore carry an order creation in `changes` and an empty 504 response in `transport`. `transport.state` reports what the server observed when sending; client receipt is captured by the client.

The runner adds the scripted conversation, approval decisions, and the Iris state and trace available through its public API. The sandbox request ID identifies a single transport attempt. Cross-service matching at call granularity requires upstream correlation metadata. Episode state is in memory, so export it before restarting the service.

## Test

```sh
python -m pip install -r requirements.txt 'pytest>=8,<10'
python -m pytest -q
```
