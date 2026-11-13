# Sandbox

A stateful simulation environment for testing workflows and capturing execution traces.

Sandbox runs simulated services independently of the application under test. The first adapter is a synthetic McDelivery service with 13 MCP tools for delivery addresses, stores, menus, coupons, quotes, and orders. The episode API owns isolation, virtual time, fault injection, and trace export. The optional runner drives Iris through its existing chat and approval APIs.

## Run the sandbox

Use Python 3.11 or newer from the repository root. Copy `.env.example` to `.env` on first setup and fill in the values for the commands you use. Sandbox, Runner, the regression suite, and Monitor reclassification read `.env` from the Sandbox checkout. Command-line flags override the corresponding environment defaults, and exported variables override the same keys in `.env`. The local file is gitignored.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

For manual use, generate an admin token with `openssl rand -hex 24` and set `SANDBOX_ADMIN_TOKEN` in `.env` or export it before starting the service.

```sh
python -m sandbox.server
```

The service listens on `127.0.0.1:8100`. The admin bearer token manages episodes. Generate a fresh MCP bearer token for each episode. Start with `--archive-dir runs/sandbox-archive` to retain closed snapshots and token claims across restarts. Use one server process per archive directory.

To open an episode and list its tools without Iris, run the following in another terminal. Export the same admin token there for `curl`, and generate a fresh MCP token.

```sh
export SANDBOX_ADMIN_TOKEN='<same admin token>'
export MCD_MCP_TOKEN="$(openssl rand -hex 24)"
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

Start a dedicated Iris test instance with its own PostgreSQL, Redis, and model configuration. Generate an episode ID and token before launching Iris, and use the same values in the Iris and Runner terminals.

```sh
export SANDBOX_EPISODE_ID="$(openssl rand -hex 16)"
export MCD_MCP_TOKEN="$(openssl rand -hex 24)"
export MCD_MCP_URL="http://127.0.0.1:8100/mcp/$SANDBOX_EPISODE_ID/1"
export IRIS_AUTO_REVIEW=false
```

With Sandbox and Iris running, start the runner from the Sandbox repository. If you use a separate terminal for the runner, export the same `SANDBOX_EPISODE_ID` and `MCD_MCP_TOKEN` there or set them in the Sandbox `.env`. The runner opens each episode, asks Iris to query delivery addresses, and checks for that call in the new episode before sending the scenario prompt. It approves a matching create-order request at most once, after verifying the quote and budget.

```sh
python -m runner order --iris http://127.0.0.1:8080/api
```

Each invocation runs one scenario. To run another scenario, create a fresh ID and token and restart Iris with those values. Iris caches the MCP client configuration for its process. The Runner retries an ambiguous episode-open response using the same ID. It writes `evidence.json` and `events.jsonl` under `runs/<batch>/<scenario>/` and exits nonzero for a failed or unavailable verdict.

The timeout scenarios verify the injected response, the order state and Iris ledger, a read-only lookup after the timeout, and the user-visible conclusion. Evidence groups lookups by whether they started before the timeout, after the timeout without a user prompt, or after the scripted follow-up. A prompted lookup can satisfy the scenario and is recorded separately from autonomous recovery. The verdict also checks explicit amount, payment, and delivery claims. Provide `--iris-revision` to record the build revision alongside the observed model names, scenario settings, and trace limitations.

### Run the local regression suite

Use a dedicated local PostgreSQL database with pgvector, a local Redis instance, and an Iris checkout with its model credentials configured in the Iris `.env` or process environment. In the Sandbox `.env`, set `IRIS_REGRESSION_REPO` and the local database credentials. Set `MONITOR_DASHSCOPE_API_KEY` there to include the clarification scenarios. Relative values for `IRIS_REGRESSION_REPO` are resolved from the Sandbox root. The suite initializes the Iris schema and uses the test ports, database, and Redis database shown in `.env.example`. Java 17 and Maven must be on PATH.

```sh
python -m runner.regression
```

Pass scenario names such as `timeout-before clarify-address` to run a subset. The full suite runs quote, order, denial, budget, both timeout variants, and both clarification scenarios. The clarification cases use Monitor active mode. The command builds Iris once, starts Sandbox on a loopback port, and runs each scenario in a fresh Iris process with its own MCP credentials. It generates the admin token, episode IDs, and MCP tokens and shuts down the processes it starts. To override the configured checkout for a run, pass `--iris-repo /path/to/Iris`.

The batch `summary.json` links to each case's evidence and lists the verdict and failed checks. The per-case `summary.json` shows the initial query, Iris replies observed by Monitor, matched intents and their evidence, the actual scripted follow-ups, and the final reply. A missing Iris trace is listed under diagnostics. A missing Iris ledger or verified Sandbox snapshot makes the verdict unavailable. Run artifacts and server logs are stored under `runs/regression/` unless overridden with `--out`.

### Run the intent monitor

`monitor` classifies user-visible dialogue with Bailian `qwen3.8-flash`. It receives the latest Iris reply, a bounded dialogue history, and the intent definitions for the current turn. The runner maps validated matches to scenario-owned replies. Iris approvals still use the pending ledger entry and deterministic scenario checks.

```sh
python -m runner clarify-address --monitor active
```

`DASHSCOPE_API_KEY` can supply the key if `MONITOR_DASHSCOPE_API_KEY` is unset. Set `MONITOR_BASE_URL` to use a workspace-specific OpenAI-compatible base URL.

- `active` uses verified matches for configured replies. An uncertain or unavailable classification ends automatic follow-up and preserves the reason in the evidence.
- `shadow` records the model proposal and uses the keyword baseline to select from the same scenario replies.
- `off` is the default. It uses the keyword baseline for configured replies without invoking the model. The clarification scenarios require the expected dialogue before an order can pass.

Each decision links its exact model input and recognition result to the Iris message and the actual user reply in `evidence.json`. Replay the recorded decision locally or ask Qwen to classify the captured input again.

```sh
python -m monitor runs/<batch>/<scenario>/evidence.json
python -m monitor runs/<batch>/<scenario>/evidence.json --reclassify
```

## Manage episodes and export traces

All paths in this table use `SANDBOX_ADMIN_TOKEN` as a bearer token.

| Method and path | Behavior |
| --- | --- |
| `GET /admin/catalog` | List domains and scenes. |
| `GET /admin/episodes` | List active, closed, and archived episodes by ID. |
| `POST /admin/episodes` | Open an episode with a fresh `token`, optional `episodeId`, `domain`, `scene`, `seed`, and `faults`. Repeating the same explicit ID and parameters returns the original ID within the running service. |
| `POST /admin/episodes/{id}/round` | Set the round using `{"round": 1}`. Include `generation` to require the matching generation. |
| `GET /admin/episodes/{id}/snapshot` | Read the fault plan, initial and current state, and call events. |
| `GET /admin/episodes/{id}/events` | Export tool events as newline-delimited JSON. |
| `POST /admin/episodes/{id}/reset` | Restore the initial state and fault plan, clear calls, and increment the generation. |
| `POST /admin/episodes/{id}/close` | Seal the episode, return its final snapshot, and retire the bearer token. |
| `DELETE /admin/episodes/{id}` | Remove a closed episode or its on-disk archive. |

Round changes, resets, and closes return HTTP 409 while an admitted MCP POST is in flight. If archive publication fails, the episode remains active so the administrator can retry closing it.

The Runner pins the generation for round changes, snapshots, and closing. Manual clients can use `/mcp` in generation 1. After a reset, use `/mcp/<episodeId>/<generation>` with the new generation reported by the reset response. A retired token cannot open a new episode.

To simulate an order that committed before timing out, pass this fault plan when opening an episode.

```json
{
  "faults": [{"type": "TIMEOUT_COMMITTED", "tool": "create-order", "nth": 1}]
}
```

`TIMEOUT_COMMITTED` and `TIMEOUT_NOT_COMMITTED` both return HTTP 504. The former runs the tool before substituting the 504. `CONNECTION_FAIL` returns 502 and `AUTH_401` returns 401. Transport faults also accept `mode: "rpc-error"` to emit a JSON-RPC error under HTTP 200, and `delaySeconds` to delay the response. Business faults `SOLD_OUT`, `STORE_CLOSED`, and `COUPON_INVALID` take a product, store, or coupon ID in `target`. They can apply at episode start via `"at": "start"`. Faults can target a named tool or all calls via `"*"`, and `persist: true` repeats a fault from its configured call onward.

Each version 1 `tool.call` event includes the run ID, generation, request ID, JSON-RPC ID, arguments, optional MCP metadata, triggered and skipped faults, business result, entity changes, and transport observations. Entity changes contain the before and after values. A delayed timeout can record an order creation, a planned HTTP 504, and a disconnect with no submitted HTTP status. `emitted` means the response was handed to the ASGI server. Client receipt requires client-side evidence.

The runner adds the scripted conversation, approval decisions, and the Iris state and trace available through its public API. The sandbox request ID identifies a single transport attempt. Cross-service matching at call granularity requires upstream correlation metadata. Active episodes are in memory. When archiving is enabled, closing an episode publishes its final snapshot on disk.

## Test

```sh
python -m pip install -r requirements.txt 'pytest>=8,<10'
python -m pytest -q
```
