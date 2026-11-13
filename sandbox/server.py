"""Streamable HTTP MCP and the episode management API."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest

import anyio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route

from sandbox.domains.mcd import McDelivery
from sandbox.episodes import Domain, EpisodeConflict, EpisodeStore, Fault, HTTP_FAULTS

log = logging.getLogger(__name__)


class OpenEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(pattern=r"^[\x21-\x7e]+$", max_length=512)
    episodeId: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    domain: str = "mcd"
    scene: str = "lunch"
    seed: int = 0
    faults: list[Fault] = Field(default_factory=list)


class SetRound(BaseModel):
    round: int = Field(ge=0)
    generation: int | None = Field(default=None, ge=1)


def bearer(headers: Headers) -> str:
    scheme, _, value = headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def create_app(*, admin_token: str | None = None, domains: list[Domain] | None = None,
               archive_dir: Path | None = None) -> Starlette:
    admin_token = admin_token if admin_token is not None else os.environ.get("SANDBOX_ADMIN_TOKEN", "")
    if not admin_token or not admin_token.isascii() or any(not 33 <= ord(char) <= 126 for char in admin_token):
        raise ValueError("set SANDBOX_ADMIN_TOKEN to a nonempty ASCII bearer token")
    registry = {domain.name: domain for domain in (domains if domains is not None else [McDelivery()])}
    store = EpisodeStore(archive_dir)
    server = Server("sandbox")

    @server.list_tools()
    async def tools() -> list[types.Tool]:
        episode = server.request_context.request.scope["sandbox.episode"]
        return [types.Tool.model_validate(tool) for tool in episode.domain.tools]

    @server.call_tool(validate_input=False)
    async def call(name: str, arguments: dict) -> types.CallToolResult:
        context = server.request_context
        scope = context.request.scope
        episode = scope["sandbox.episode"]
        meta = context.meta.model_dump(by_alias=True, exclude_none=True) if context.meta is not None else None
        if meta and (meta.get("runId", episode.episode_id) != episode.episode_id
                     or meta.get("generation", episode.generation) != episode.generation):
            return types.CallToolResult(content=[types.TextContent(
                type="text", text="Request metadata does not match the active episode",
            )], isError=True)
        event, fault = episode.invoke(name, arguments, context.request_id, meta)
        scope["sandbox.call"] = event
        if fault is not None:
            scope["sandbox.fault"] = fault
        business = event["business"]
        text = business.get("text", "Tool execution interrupted") if fault is None else "Injected transport fault"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            # Business errors are represented by the provider's JSON envelope.
            isError=fault is not None or "error" in business,
        )

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "[::1]"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        ),
    )

    async def mcp_endpoint(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"].rstrip("/")
        scoped = re.fullmatch(r"/mcp/([0-9a-f]{32})/([1-9][0-9]{0,9})", path)
        if path not in ("", "/mcp") and not scoped:
            await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
            return
        token = bearer(Headers(scope=scope))
        admitted = scope["method"] == "POST"
        episode_scope = (scoped[1], int(scoped[2])) if scoped else None
        episode = store.admit(token, episode_scope) if admitted else store.bound(token)
        if episode is None or (not admitted and (episode_scope or (episode.episode_id, 1))
                               != (episode.episode_id, episode.generation)):
            await JSONResponse({"error": "no active episode for bearer token"}, status_code=401)(
                scope, receive, send,
            )
            return
        scope["sandbox.episode"] = episode

        status, emitted = None, False
        parts = []
        substituted = False
        disconnected = False

        async def receive_observed():
            nonlocal disconnected
            message = await receive()
            disconnected |= message["type"] == "http.disconnect"
            return message

        async def send_observed(message):
            nonlocal status, emitted
            if scope.get("sandbox.call") is not None:
                # The SDK has consumed the POST body. Check for an already queued
                # disconnect before submitting response bytes to the ASGI server.
                with anyio.CancelScope() as poll:
                    poll.cancel()
                    await receive_observed()
                if disconnected:
                    return
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                parts.append(message.get("body", b""))
                emitted = not message.get("more_body", False) and not disconnected
                event = scope.get("sandbox.call")
                if emitted and event is not None:
                    episode.finish(event, status, b"".join(parts).decode("utf-8"), "emitted")

        async def send_with_fault(message):
            nonlocal substituted
            fault = scope.get("sandbox.fault")
            if fault is None:
                await send_observed(message)
            elif not substituted and message["type"] == "http.response.start":
                substituted = True
                await anyio.sleep(fault.delaySeconds)
                status_code = HTTP_FAULTS[fault.type]
                if fault.mode == "rpc-error":
                    response = JSONResponse({
                        "jsonrpc": "2.0",
                        "id": scope["sandbox.call"]["request"]["rpcId"],
                        "error": {"code": -32603, "message": f"Sandbox transport failure ({status_code})"},
                    })
                else:
                    response = Response(status_code=status_code)
                await response(scope, receive_observed, send_observed)

        try:
            await manager.handle_request(scope, receive_observed, send_with_fault)
        finally:
            event = scope.get("sandbox.call")
            with episode.lock:
                if event is not None and not emitted:
                    state = "disconnected" if disconnected else "incomplete"
                    episode.finish(event, status, b"".join(parts).decode("utf-8") if parts else None, state)
                if admitted:
                    episode.in_flight -= 1

    def admin_authenticated(request: Request) -> bool:
        return compare_digest(bearer(request.headers).encode(), admin_token.encode())

    async def catalog(request: Request) -> Response:
        if not admin_authenticated(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"domains": {name: domain.scenes() for name, domain in registry.items()}})

    async def open_episode(request: Request) -> Response:
        if not admin_authenticated(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.method == "GET":
            return JSONResponse({"episodes": store.list()})
        try:
            config = OpenEpisode.model_validate_json(await request.body())
            domain = registry.get(config.domain)
            if domain is None or config.scene not in domain.scenes():
                raise ValueError("unknown domain or scene")
            if compare_digest(config.token, admin_token):
                raise ValueError("episode and admin tokens must differ")
            episode_id, created = store.create(config.token, domain, config.scene, config.seed, config.faults,
                                               config.episodeId)
        except ValidationError as error:
            return JSONResponse({"errors": error.errors(include_input=False, include_context=False)}, status_code=400)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except EpisodeConflict as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except OSError:
            log.exception("Could not persist the episode credential")
            return JSONResponse({"error": "could not open episode"}, status_code=503)
        return JSONResponse({"episodeId": episode_id, "runId": episode_id}, status_code=201 if created else 200)

    async def episode_action(request: Request) -> Response:
        if not admin_authenticated(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        episode_id = request.path_params["episode_id"]
        action = request.path_params.get("action")
        generation = request.query_params.get("generation")
        if generation is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,9}", generation):
                return JSONResponse({"error": "invalid episode generation"}, status_code=400)
            generation = int(generation)
        episode = store.get(episode_id)
        if episode is None:
            try:
                if request.method == "DELETE" and await run_in_threadpool(store.delete_archive, episode_id):
                    return Response(status_code=204)
                archived = await run_in_threadpool(store.archived, episode_id)
            except (OSError, ValueError):
                log.exception("Could not access episode archive %s", episode_id)
                return JSONResponse({"error": "episode archive unavailable"}, status_code=503)
            if archived is None:
                return JSONResponse({"error": "episode not found"}, status_code=404)
            if (action, request.method) in {("snapshot", "GET"), ("close", "POST")}:
                if generation is not None and generation != archived["generation"]:
                    return JSONResponse({"error": "episode generation changed"}, status_code=409)
                return JSONResponse(archived)
            if action == "events" and request.method == "GET":
                return event_stream(archived["calls"])
            if action in {"reset", "round"} and request.method == "POST":
                return JSONResponse({"error": "episode is closed"}, status_code=409)
            return JSONResponse({"error": "unknown episode action"}, status_code=404)
        if request.method == "DELETE":
            try:
                store.delete(episode)
            except EpisodeConflict as error:
                return JSONResponse({"error": str(error)}, status_code=409)
            return Response(status_code=204)
        if request.method == "GET":
            if action == "snapshot":
                snapshot = episode.snapshot()
                if generation is not None and generation != snapshot["generation"]:
                    return JSONResponse({"error": "episode generation changed"}, status_code=409)
                return JSONResponse(snapshot)
            if action == "events":
                return event_stream(episode.events())
        if request.method == "POST":
            try:
                if action == "close":
                    try:
                        snapshot = await run_in_threadpool(store.close, episode, generation)
                    except OSError:
                        log.exception("Could not archive episode %s", episode_id)
                        return JSONResponse({"error": "could not archive episode"}, status_code=503)
                    return JSONResponse(snapshot)
                if action == "reset":
                    episode.reset()
                    return JSONResponse(episode.snapshot())
                if action == "round":
                    value = SetRound.model_validate_json(await request.body())
                    with episode.lock:
                        if episode.closed:
                            raise EpisodeConflict("episode is closed")
                        if value.generation is not None and episode.generation != value.generation:
                            raise EpisodeConflict("episode generation changed")
                        episode.ensure_idle()
                        episode.round = value.round
                    return JSONResponse({"round": value.round})
            except EpisodeConflict as error:
                return JSONResponse({"error": str(error)}, status_code=409)
            except ValidationError:
                return JSONResponse({"error": "round must be a nonnegative integer"}, status_code=400)
        return JSONResponse({"error": "unknown episode action"}, status_code=404)

    def event_stream(events: list[dict]) -> StreamingResponse:
        lines = (json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        return StreamingResponse(lines, media_type="application/x-ndjson")

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Route("/admin/catalog", catalog),
            Route("/admin/episodes", open_episode, methods=["GET", "POST"]),
            Route("/admin/episodes/{episode_id}", episode_action, methods=["DELETE"]),
            Route("/admin/episodes/{episode_id}/{action}", episode_action, methods=["GET", "POST"]),
            Mount("/", app=mcp_endpoint),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sandbox on loopback")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--archive-dir", type=Path, help="store closed episode snapshots on disk")
    args = parser.parse_args()
    uvicorn.run(create_app(archive_dir=args.archive_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
