"""Native flat-body and request-scoped tool policy seams."""

import json
from typing import Any, cast

import msgspec
from litestar import Litestar, get, post
from litestar.testing import TestClient

from litestar_mcp import LitestarMCP, MCPConfig, get_mcp_request_context

PROTOCOL_VERSION = "2026-07-28"


def _rpc(
    client: TestClient[Any],
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_headers: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    request_params = dict(params or {})
    request_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "policy-tests", "version": "1"},
        **(metadata or {}),
    }
    headers = {
        "Accept": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
        **(request_headers or {}),
    }
    if method == "tools/call":
        headers["Mcp-Name"] = str(request_params.get("name", ""))
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": request_params},
        headers=headers,
    )


def test_flat_body_opt_preserves_source_style_tool_arguments() -> None:
    class Payload(msgspec.Struct):
        title: str
        count: int

    @post("/create", mcp_tool="create", mcp_flatten_body=True, sync_to_thread=False)
    def create(data: Payload) -> dict[str, Any]:
        return {"title": data.title, "count": data.count}

    app = Litestar(route_handlers=[create], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        listed = _rpc(client, "tools/list").json()["result"]["tools"][0]
        called = _rpc(
            client,
            "tools/call",
            {"name": "create", "arguments": {"title": "Hello", "count": 2}},
        ).json()

    assert set(listed["inputSchema"]["properties"]) == {"title", "count"}
    assert listed["inputSchema"]["required"] == ["title", "count"]
    assert json.loads(called["result"]["content"][0]["text"]) == {"title": "Hello", "count": 2}


def test_tool_policy_uses_the_same_request_for_discovery_and_execution() -> None:
    class HeaderPolicy:
        @staticmethod
        def _visible(request: Any) -> str:
            return str(request.headers.get("x-visible-tool", ""))

        async def transform_tools(self, tools: list[dict[str, Any]], request: Any) -> list[dict[str, Any]]:
            visible = self._visible(request)
            return [tool for tool in tools if tool["name"] == visible]

        async def allows_tool(self, name: str, arguments: dict[str, Any], request: Any) -> bool:
            assert arguments in ({}, {"value": 1})
            return name == self._visible(request)

    @get("/one", mcp_tool="one", sync_to_thread=False)
    def one() -> str:
        return "one"

    @get("/two", mcp_tool="two", sync_to_thread=False)
    def two() -> str:
        return "two"

    app = Litestar(route_handlers=[one, two], plugins=[LitestarMCP(MCPConfig(tool_policy=HeaderPolicy()))])
    with TestClient(app=app) as client:
        listed = _rpc(client, "tools/list", request_headers={"X-Visible-Tool": "one"}).json()
        allowed = _rpc(
            client,
            "tools/call",
            {"name": "one", "arguments": {}},
            request_headers={"X-Visible-Tool": "one"},
        ).json()
        denied = _rpc(
            client,
            "tools/call",
            {"name": "two", "arguments": {}},
            request_headers={"X-Visible-Tool": "one"},
        ).json()

    assert [tool["name"] for tool in listed["result"]["tools"]] == ["one"]
    assert allowed["result"]["isError"] is False
    assert denied["error"]["code"] == -32602
    assert denied["error"]["message"] == "Tool not found: two"


def test_request_context_carries_the_complete_meta_object() -> None:
    @get("/context", mcp_tool="context", sync_to_thread=False)
    def context() -> dict[str, Any]:
        return cast("dict[str, Any]", get_mcp_request_context().metadata)

    app = Litestar(route_handlers=[context], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        response = _rpc(
            client,
            "tools/call",
            {"name": "context", "arguments": {}},
            metadata={"example.test/context": {"value": 7}},
        ).json()

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["example.test/context"] == {"value": 7}
