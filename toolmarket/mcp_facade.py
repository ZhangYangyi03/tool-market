"""An MCP front door for the resource substrate.

Why this file exists
--------------------
The shelf has 153 resources and one way in: our own REST (`GET /resources`,
`POST /resources/{id}/invoke`). That is a protocol only we speak. Every other
agent -- Claude Desktop, Cursor, any MCP client -- speaks MCP, and from where
they stand toolmarket is a 404 at `/mcp`: listening, healthy, and unusable.

This module is the translation and nothing else. It does not decide which
resources exist, whether one may run, or what it returns; it asks the registry
exactly the questions the REST routes ask and restates the answers in MCP's
vocabulary. Two front doors, one state machine -- the same rule `api/main.py`
states for REST versus gRPC. In particular a call here reaches enforcement
through `ResourceRegistry.invoke`, so the trust policy and the permission mode
still apply; a facade that called the compiled function directly would be a
second, unaudited way to run a tool, which is the bug the resource/gRPC split
already taught this codebase to avoid.

Transport: MCP "streamable HTTP", POST-only. A client POSTs JSON-RPC 2.0 and
gets `application/json` back, which the spec allows as an alternative to
`text/event-stream`. Server-initiated streams (`GET /mcp`) are not offered: no
operation here needs to push, and a half-implemented SSE endpoint would be worse
than an honest 405.

Scope, deliberately: `tools/list` and `tools/call` only. MCP's separate
`resources/*` family is file-like blobs with URIs -- a different concept that
happens to share a word with our "resource", and mapping one onto the other
would make both harder to reason about.

Auth: none, matching the REST surface. The deployment binds to loopback and the
public path runs through a tunnel that applies its own policy; a token scheme
here would be the second way in the comment in `toolmarket_server.py` warns
about.
"""
from __future__ import annotations

import json
from typing import Any, Optional

# Imported at module scope on purpose, even though only `install` needs them.
# With `from __future__ import annotations` every annotation is a *string*, and
# FastAPI resolves those against the function's module globals -- a `Request`
# imported inside `install` is invisible there, so the annotation stays the
# string "Request" and FastAPI classifies the parameter as a query field: every
# POST answers 422 "missing query: request" while the route looks correct in
# `app.routes`. Optional, as in `api/main.py`, so the core module still imports
# without the API extra.
try:
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001 - API extras are optional
    _HAVE_FASTAPI = False

# The revision this facade implements. A client's requested revision is echoed
# back when it asks for one -- MCP's negotiation is "server answers with what it
# supports", and answering with a version the client did not ask for is how a
# client silently switches to a dialect the server does not speak. Only the
# other direction is checked: a *known-old* client gets its version, anything
# unrecognised gets ours.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_PARSE_ERROR = -32700


def _rpc_result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str,
               data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _negotiate(params: dict[str, Any]) -> str:
    asked = params.get("protocolVersion")
    if isinstance(asked, str) and asked in SUPPORTED_PROTOCOL_VERSIONS:
        return asked
    return DEFAULT_PROTOCOL_VERSION


# -- resource -> tool projection -------------------------------------------
def tool_view(rec: Any) -> dict[str, Any]:
    """One ResourceRecord as an MCP tool.

    `inputSchema` comes from `as_capability_schema()`: the record already
    projects itself into exactly this shape, with `additionalProperties: false`
    for strict function calling, so building a second schema here would be the
    duplication this facade is supposed to avoid.

    The lifecycle state is carried in the description and in `_meta`, not used
    as a filter. MCP has no way to ask for a subset and a client that cannot see
    a resource cannot decide it wants to promote it; enforcement happens at call
    time, where the ledger is, and saying so in the description is more honest
    than pretending a draft tool does not exist.
    """
    schema = rec.as_capability_schema() if hasattr(rec, "as_capability_schema") \
        else {"name": rec.name, "description": rec.description,
              "parameters": rec.contract.parameters}
    state = getattr(rec.state, "value", str(rec.state))
    desc = (rec.description or "").strip()
    return {
        "name": rec.name,
        "title": rec.name,
        "description": (desc + f"  [toolmarket state: {state}]").strip(),
        "inputSchema": schema.get("parameters") or {
            "type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {"toolmarket/resource_id": rec.id,
                  "toolmarket/state": state,
                  "toolmarket/version": getattr(
                      getattr(rec, "version_current", None), "version", None)},
    }


def _tool_text(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [{"type": "text",
             "text": json.dumps(content, ensure_ascii=False, default=str)}]


# -- request handling ------------------------------------------------------
def handle(reg: Any, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One JSON-RPC message in, one response out. None for a notification.

    Returning None rather than a response is what makes `notifications/*` legal
    without a second code path: MCP defines a notification as a message with no
    `id`, and the JSON-RPC spec forbids answering one. A facade that replied to
    `notifications/initialized` would look correct in a hand test and break
    clients that treat an unexpected id as a protocol violation.
    """
    if not isinstance(msg, dict):
        return _rpc_error(None, JSONRPC_INVALID_REQUEST, "not a JSON object")
    if msg.get("jsonrpc") != "2.0":
        return _rpc_error(msg.get("id"), JSONRPC_INVALID_REQUEST,
                          "jsonrpc must be '2.0'")
    method = msg.get("method")
    if not isinstance(method, str):
        return _rpc_error(msg.get("id"), JSONRPC_INVALID_REQUEST,
                          "missing method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    is_notification = "id" not in msg

    if method in ("notifications/initialized", "notifications/cancelled",
                  "notifications/roots/list_changed"):
        return None
    if method == "initialize":
        return _rpc_result(msg_id, {
            "protocolVersion": _negotiate(params),
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "toolmarket", "version": "0.1.0"},
            "instructions": (
                "A shelf of registered agent tools. tools/list returns every "
                "resource with its lifecycle state; tools/call runs one through "
                "the same enforcement the REST surface uses."),
        })
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        tools = [tool_view(r) for r in reg.list(type="tool")]
        return _rpc_result(msg_id, {"tools": tools})
    if method == "tools/call":
        return _call(reg, msg_id, params)
    if is_notification:
        return None
    return _rpc_error(msg_id, JSONRPC_METHOD_NOT_FOUND,
                      f"unknown method: {method}")


def _call(reg: Any, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not name:
        return _rpc_error(msg_id, JSONRPC_INVALID_PARAMS, "name is required")
    if not isinstance(arguments, dict):
        return _rpc_error(msg_id, JSONRPC_INVALID_PARAMS,
                          "arguments must be an object")

    rid = f"tool:{name}"
    if reg.get(rid) is None:
        # Fall back to the display name: a resource registered under a
        # different id would otherwise be listed and uncallable, which is the
        # worst of the two failure modes because tools/list said it existed.
        match = next((r for r in reg.list(type="tool") if r.name == name), None)
        if match is None:
            return _rpc_error(msg_id, JSONRPC_INVALID_PARAMS,
                              f"unknown tool: {name}")
        rid = match.id

    # An unknown tool is a protocol error (above) and a failed run is not: the
    # spec puts execution failures in the result with `isError`, so a client can
    # tell "you called it wrong" from "it ran and returned an error" and hand
    # the second to the model instead of aborting.
    try:
        result = reg.invoke(rid, arguments)
    except Exception as exc:  # noqa: BLE001 - enforcement refusals land here
        return _rpc_result(msg_id, {
            "content": _tool_text(f"{type(exc).__name__}: {exc}"),
            "isError": True,
        })
    ok = bool(getattr(result, "ok", True))
    error = getattr(result, "error", None)
    output = getattr(result, "output", None)
    if not ok and error is not None:
        return _rpc_result(msg_id, {
            "content": _tool_text(str(error)), "isError": True,
        })
    payload: dict[str, Any] = {"content": _tool_text(output),
                               "isError": not ok}
    if isinstance(output, dict):
        payload["structuredContent"] = output
    return _rpc_result(msg_id, payload)


def install(app: Any, reg: Any, path: str = "/mcp") -> None:
    """Mount the facade on a FastAPI app. POST only, by design."""
    if not _HAVE_FASTAPI:
        raise RuntimeError("FastAPI is required for the MCP facade")

    @app.post(path)
    async def mcp(request: Request) -> Any:
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - malformed body is a client bug
            return JSONResponse(
                _rpc_error(None, JSONRPC_PARSE_ERROR, "invalid JSON"),
                status_code=400)

        batch = isinstance(payload, list)
        messages = payload if batch else [payload]
        responses: list[dict[str, Any]] = []
        for msg in messages:
            reply = handle(reg, msg)
            if reply is not None:
                responses.append(reply)

        if not responses:
            # Every message was a notification. 202 with no body is the spec's
            # answer; a 200 with an empty object would be read by some clients
            # as a response to a message that never had an id.
            return Response(status_code=202)
        body = responses if batch else responses[0]
        return JSONResponse(body)

    @app.get(path)
    def mcp_get() -> Any:
        """405, not a stub stream. See the module docstring."""
        return JSONResponse(
            {"error": "this facade is POST-only (no server-initiated stream); "
                      "POST JSON-RPC 2.0 here instead"},
            status_code=405)
