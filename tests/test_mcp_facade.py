"""The MCP facade: a second front door that must reach the same state machine.

Three things are worth a test and are tested here.

1. Shape. `initialize` / `tools/list` / `tools/call` answer in MCP's vocabulary,
   and a message with no `id` gets no response at all -- replying to a
   notification is a protocol violation some clients abort on.

2. It is a *translation*, not a second implementation. A call must go through
   `ResourceRegistry.invoke`: enforcement, the ledger write and the trust
   reconcile all belong to the registry, and a facade that reached the compiled
   function directly would be an unaudited way to run a tool. That is asserted
   by counting registry calls, not by reading the code.

3. Failure modes are the spec's, not ours: an unknown tool is a JSON-RPC error
   (the client called it wrong) while a tool that runs and returns an error is a
   result with `isError` (the model should see it).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from toolmarket import mcp_facade as facade
from toolmarket.api.main import create_app
from toolmarket.cache import reset_cache_singleton
from toolmarket.registry import ResourceRegistry
from toolmarket.store import ResourceStore


def _spec(name: str = "slugify"):
    from autoforge.tools.spec import ToolSpec, TriggerProbe

    def slugify(text: str = "") -> str:
        return "-".join(text.lower().split())

    return ToolSpec(
        name=name,
        description="Slugify a string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        fn=slugify,
        code="def slugify(text=''):\n    return '-'.join(text.lower().split())\n",
        source="human",
        probes=[TriggerProbe(query="slugify this", expect="call")],
        invariances=["text"],
        effect_signature="pure",
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TOOLMARKET_STORE", raising=False)
    monkeypatch.setenv("RATE_LIMIT", "100000")
    reset_cache_singleton()
    yield
    reset_cache_singleton()


def _client(name="slugify", register=True):
    reg = ResourceRegistry(ResourceStore(":memory:"))
    rid = reg.register(_spec(name)).id if register else None
    return TestClient(create_app(reg), raise_server_exceptions=False), reg, rid


def _rpc(client, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body)


# -- shape -----------------------------------------------------------------
def test_initialize_negotiates_and_names_itself():
    client, _, _ = _client()
    r = _rpc(client, "initialize",
             {"protocolVersion": "2025-03-26", "capabilities": {},
              "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-03-26"
    assert res["serverInfo"]["name"] == "toolmarket"
    assert "tools" in res["capabilities"]


def test_unknown_protocol_version_falls_back_to_ours():
    client, _, _ = _client()
    r = _rpc(client, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == facade.DEFAULT_PROTOCOL_VERSION


def test_tools_list_projects_contract_into_input_schema():
    client, reg, rid = _client()
    r = _rpc(client, "tools/list")
    tools = r.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["slugify"]
    tool = tools[0]
    assert tool["inputSchema"]["properties"]["text"]["type"] == "string"
    # Strict function calling: AGP requires the record to close its schema.
    assert tool["inputSchema"]["additionalProperties"] is False
    assert tool["_meta"]["toolmarket/resource_id"] == rid
    assert tool["_meta"]["toolmarket/state"] == reg.get(rid).state.value


def test_notification_gets_no_response():
    client, _, _ = _client()
    r = _rpc(client, "notifications/initialized", None, msg_id=None)
    assert r.status_code == 202
    assert r.content == b""


def test_batch_is_answered_positionally_and_notifications_dropped():
    client, _, _ = _client()
    r = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "method": "ping", "id": 1},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "tools/list", "id": 2},
    ])
    body = r.json()
    assert isinstance(body, list) and len(body) == 2
    assert [m["id"] for m in body] == [1, 2]


def test_get_is_405_not_a_broken_stream():
    client, _, _ = _client()
    assert client.get("/mcp").status_code == 405


# -- it reaches the real state machine -------------------------------------
def test_call_goes_through_the_registry(monkeypatch):
    client, reg, rid = _client()
    seen = {"n": 0}
    real = reg.invoke

    def counting(resource_id, arguments, **kw):
        seen["n"] += 1
        return real(resource_id, arguments, **kw)

    monkeypatch.setattr(reg, "invoke", counting)
    r = _rpc(client, "tools/call",
             {"name": "slugify", "arguments": {"text": "A B"}})
    assert seen["n"] == 1, "the facade must not bypass ResourceRegistry.invoke"
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "a-b"


def test_call_is_recorded_in_the_ledger():
    client, reg, rid = _client()
    before = len(reg.event_log(rid))
    _rpc(client, "tools/call", {"name": "slugify", "arguments": {"text": "x"}})
    assert len(reg.event_log(rid)) == before + 1


# -- failure modes ---------------------------------------------------------
def test_unknown_tool_is_a_jsonrpc_error():
    client, _, _ = _client()
    r = _rpc(client, "tools/call", {"name": "nope", "arguments": {}})
    err = r.json()["error"]
    assert err["code"] == facade.JSONRPC_INVALID_PARAMS
    assert "nope" in err["message"]


def test_bad_arguments_come_back_as_iserror_not_a_protocol_error():
    client, _, _ = _client()
    r = _rpc(client, "tools/call",
             {"name": "slugify", "arguments": {"text": 5}})
    result = r.json()["result"]
    assert "error" not in r.json()
    assert result["isError"] is True
    assert result["content"][0]["text"]


def test_unknown_method_is_method_not_found():
    client, _, _ = _client()
    r = _rpc(client, "tools/wat")
    assert r.json()["error"]["code"] == facade.JSONRPC_METHOD_NOT_FOUND


def test_malformed_body_is_400_parse_error():
    client, _, _ = _client()
    r = client.post("/mcp", content=b"{not json")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == facade.JSONRPC_PARSE_ERROR


def test_empty_shelf_lists_zero_tools_not_an_error():
    client, _, _ = _client(register=False)
    r = _rpc(client, "tools/list")
    assert r.json()["result"]["tools"] == []
    assert "error" not in r.json()
