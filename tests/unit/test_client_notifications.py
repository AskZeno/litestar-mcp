"""Client JSON-RPC notifications and streamable-tools advertisement."""

from typing import Any

import pytest
from litestar import Litestar, get, post
from litestar.testing import TestClient

from litestar_mcp import LitestarMCP, MCPConfig, MCPStreamableToolsConfig
from litestar_mcp.services.handler import MCPRequestContext, get_mcp_request_context

pytestmark = pytest.mark.unit

STREAMABLE = "law.zeno/streamable-tools"


class _RecordingPolicy:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str, str | None]] = []
        self.allowed: list[str] = []

    async def transform_tools(self, tools: list[dict[str, Any]], request: object) -> list[dict[str, Any]]:
        del request
        return tools

    async def allows_tool(self, name: str, arguments: dict[str, Any], request: object) -> bool:
        del arguments, request
        self.allowed.append(name)
        return True

    async def receive_input_cancelled(
        self,
        name: str,
        stream_id: str,
        reason: str | None,
        context: MCPRequestContext,
    ) -> None:
        del context
        self.cancelled.append((name, stream_id, reason))


def _app(*, policy: _RecordingPolicy | None = None, streamable: bool = True, calls: list[bool] | None = None) -> Litestar:
    seen = calls if calls is not None else []

    @post("/create", mcp_tool="create_document", mcp_input_partial=True, sync_to_thread=False)
    def create_document() -> dict[str, str]:
        seen.append(get_mcp_request_context().is_partial)
        return {"ok": "yes"}

    @get("/ping", mcp_tool="ping_tool", sync_to_thread=False)
    def ping_tool() -> dict[str, str]:
        seen.append(get_mcp_request_context().is_partial)
        return {"ok": "pong"}

    config = MCPConfig(
        streamable_tools=MCPStreamableToolsConfig(extension=STREAMABLE) if streamable else None,
        tool_policy=policy,
    )
    return Litestar(route_handlers=[create_document, ping_tool], plugins=[LitestarMCP(config)])


def _discover(client: TestClient[Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]  # type: ignore[no-any-return]


def test_unknown_client_notification_is_accepted() -> None:
    with TestClient(app=_app()) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert response.status_code == 202
    assert response.content == b""


def test_streamable_tools_are_advertised_on_discover_and_list() -> None:
    with TestClient(app=_app()) as client:
        discover = _discover(client)
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert listed.status_code == 200
    assert discover["capabilities"]["extensions"][STREAMABLE] == {}
    tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    assert tools["create_document"]["_meta"][STREAMABLE] == {"inputPartial": True}
    assert STREAMABLE not in tools["ping_tool"].get("_meta", {})


def test_input_partial_runs_advertised_handler_with_is_partial() -> None:
    policy = _RecordingPolicy()
    calls: list[bool] = []
    with TestClient(app=_app(policy=policy, calls=calls)) as client:
        accepted = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/tools/input_partial",
                "params": {
                    "name": "create_document",
                    "streamId": "s1",
                    "arguments": {"title": "NDA"},
                },
            },
        )
        ignored = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/tools/input_partial",
                "params": {
                    "name": "ping_tool",
                    "streamId": "s2",
                    "arguments": {"x": 1},
                },
            },
        )
        complete = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "create_document", "arguments": {}},
            },
        )

    assert accepted.status_code == 202
    assert ignored.status_code == 202
    assert complete.status_code == 200
    assert calls == [True, False]
    assert policy.allowed == ["create_document", "create_document"]


def test_input_partial_stream_id_from_meta() -> None:
    calls: list[bool] = []
    with TestClient(app=_app(calls=calls)) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/tools/input_partial",
                "params": {
                    "name": "create_document",
                    "arguments": {"content": "Hello"},
                    "_meta": {f"{STREAMABLE}/streamId": "s-meta"},
                },
            },
        )

    assert response.status_code == 202
    assert calls == [True]


def test_input_cancelled_reaches_policy() -> None:
    policy = _RecordingPolicy()
    with TestClient(app=_app(policy=policy)) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/tools/input_cancelled",
                "params": {"name": "create_document", "streamId": "s1", "reason": "stop"},
            },
        )

    assert response.status_code == 202
    assert policy.cancelled == [("create_document", "s1", "stop")]
