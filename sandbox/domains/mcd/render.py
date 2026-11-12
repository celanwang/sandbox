"""MCD's business response envelope is carried in a single MCP text item."""

import hashlib

from sandbox.episodes import Episode


def envelope(ep: Episode, data=None, *, success=True, code=200, message="请求成功") -> dict:
    trace = f"{ep.scene_id}:{ep.seed}:{ep.seq + 1}"
    result = {
        "success": success,
        "code": code,
        "message": message,
        "datetime": ep.world["clock"],
        "traceId": hashlib.sha256(trace.encode()).hexdigest()[:32],
    }
    if data is not None or success:
        result["data"] = data
    return result
