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


def test_tool_policy_transform_arguments_injects_trusted_values_before_dispatch() -> None:
    """A policy may rewrite call arguments after authorization, e.g. to supply
    a required path parameter from verified request identity that
    ``transform_tools`` hid from the advertised schema.
    """

    class InjectingPolicy:
        async def transform_tools(self, tools: list[dict[str, Any]], request: Any) -> list[dict[str, Any]]:
            del request
            return tools

        async def allows_tool(self, name: str, arguments: dict[str, Any], request: Any) -> bool:
            del name, request
            return "tenant" not in arguments

        async def transform_arguments(self, name: str, arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            del name
            return {**arguments, "tenant": str(request.headers.get("x-verified-tenant", ""))}

    @post("/echo/{tenant:str}", mcp_tool="echo", sync_to_thread=False)
    def echo(data: dict[str, Any], tenant: str) -> dict[str, Any]:
        return {"tenant": tenant, "data": data}

    app = Litestar(route_handlers=[echo], plugins=[LitestarMCP(MCPConfig(tool_policy=InjectingPolicy()))])
    with TestClient(app=app) as client:
        smuggled = _rpc(
            client,
            "tools/call",
            {"name": "echo", "arguments": {"tenant": "ws-evil", "data": {"x": 1}}},
            request_headers={"x-verified-tenant": "ws-good"},
        ).json()
        injected = _rpc(
            client,
            "tools/call",
            {"name": "echo", "arguments": {"data": {"x": 1}}},
            request_headers={"x-verified-tenant": "ws-good"},
        ).json()

    assert "error" in smuggled or smuggled["result"].get("isError") is True
    payload = json.loads(injected["result"]["content"][0]["text"])
    assert payload == {"tenant": "ws-good", "data": {"x": 1}}


def test_a_handler_may_return_the_specification_result_model_directly() -> None:
    """A handler that builds an MCP result itself knows the wire format
    better than the plugin can infer it, so its output is forwarded whole:
    structured content, provenance blocks, and all.
    """

    class SpecResult:
        """Shaped like the published CallToolResult model."""

        content = [
            {"type": "text", "text": '{"id":"7"}'},
            {"type": "resource_link", "uri": "sheets://table/7", "name": "QA table"},
        ]
        structured_content = {"id": "7"}
        is_error = False

        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "content": [
                    {"type": "text", "text": '{"id":"7"}'},
                    {"type": "resource_link", "uri": "sheets://table/7", "name": "QA table"},
                ],
                "structuredContent": {"id": "7"},
                "isError": False,
            }

    @post("/build", mcp_tool="build", sync_to_thread=False)
    def build() -> "Any":
        return SpecResult()

    app = Litestar(route_handlers=[build], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        result = _rpc(client, "tools/call", {"name": "build", "arguments": {}}).json()["result"]

    assert result["structuredContent"] == {"id": "7"}
    assert [block["type"] for block in result["content"]] == ["text", "resource_link"]
    assert result["isError"] is False


def test_resource_reads_go_through_the_same_policy_as_tool_calls() -> None:
    """A resource is dispatched to a handler like a tool is, so it needs the
    same narrowing: discovery is filtered, a denied read reads as not found,
    and values the request proves are injected rather than taken from the URI.
    """

    class TenantPolicy:
        @staticmethod
        def _tenant(request: Any) -> str:
            return str(request.headers.get("x-verified-tenant", ""))

        async def transform_tools(self, tools: list[dict[str, Any]], request: Any) -> list[dict[str, Any]]:
            del request
            return tools

        async def allows_tool(self, name: str, arguments: dict[str, Any], request: Any) -> bool:
            del name, arguments, request
            return True

        async def transform_resources(self, entries: list[dict[str, Any]], request: Any) -> list[dict[str, Any]]:
            return entries if self._tenant(request) else []

        async def allows_resource(self, uri: str, arguments: dict[str, Any], request: Any) -> bool:
            del uri
            return bool(self._tenant(request)) and "tenant" not in arguments

        async def transform_resource_arguments(
            self, uri: str, arguments: dict[str, Any], request: Any
        ) -> dict[str, Any]:
            del uri
            return {**arguments, "tenant": self._tenant(request)}

    @get(
        "/things/{tenant:str}/{thing_id:str}",
        mcp_resource="thing",
        mcp_resource_template="app://things/{thing_id}",
        sync_to_thread=False,
    )
    def read_thing(tenant: str, thing_id: str) -> dict[str, str]:
        return {"tenant": tenant, "thing_id": thing_id}

    app = Litestar(route_handlers=[read_thing], plugins=[LitestarMCP(MCPConfig(tool_policy=TenantPolicy()))])
    with TestClient(app=app) as client:
        authenticated = {"x-verified-tenant": "acme"}
        listed = _rpc(client, "resources/templates/list", request_headers=authenticated).json()["result"]
        hidden = _rpc(client, "resources/templates/list").json()["result"]
        read = _rpc(client, "resources/read", {"uri": "app://things/42"}, request_headers=authenticated).json()
        denied = _rpc(client, "resources/read", {"uri": "app://things/42"}).json()

    assert [entry["uriTemplate"] for entry in listed["resourceTemplates"]] == ["app://things/{thing_id}"]
    assert hidden["resourceTemplates"] == [], "an unauthenticated request discovers nothing"
    assert json.loads(read["result"]["contents"][0]["text"]) == {"tenant": "acme", "thing_id": "42"}
    assert "error" in denied, "a denied read reports not found, never the resource"
