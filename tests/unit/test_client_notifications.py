"""Client JSON-RPC notifications and streamable-tools advertisement."""

from typing import Any

import pytest
from litestar import Litestar, get, post
from litestar.testing import TestClient

from litestar_mcp import LitestarMCP, MCPConfig, MCPStreamableToolsConfig
from litestar_mcp.services.handler import MCPRequestContext

pytestmark = pytest.mark.unit

STREAMABLE = "law.zeno/streamable-tools"


class _RecordingPolicy:
    def __init__(self) -> None:
        self.partials: list[tuple[str, dict[str, Any], str]] = []
        self.cancelled: list[tuple[str, str, str | None]] = []

    async def transform_tools(self, tools: list[dict[str, Any]], request: object) -> list[dict[str, Any]]:
        del request
        return tools

    async def allows_tool(self, name: str, arguments: dict[str, Any], request: object) -> bool:
        del name, arguments, request
        return True

    async def receive_input_partial(
        self,
        name: str,
        arguments: dict[str, Any],
        stream_id: str,
        context: MCPRequestContext,
    ) -> None:
        del context
        self.partials.append((name, arguments, stream_id))

    async def receive_input_cancelled(
        self,
        name: str,
        stream_id: str,
        reason: str | None,
        context: MCPRequestContext,
    ) -> None:
        del context
        self.cancelled.append((name, stream_id, reason))


def _app(*, policy: _RecordingPolicy | None = None, streamable: bool = True) -> Litestar:
    @post("/create", mcp_tool="create_document", mcp_input_partial=True, sync_to_thread=False)
    def create_document() -> dict[str, str]:
        return {"ok": "yes"}

    @get("/ping", mcp_tool="ping_tool", sync_to_thread=False)
    def ping_tool() -> dict[str, str]:
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


def test_input_partial_reaches_policy_only_for_advertised_tools() -> None:
    policy = _RecordingPolicy()
    with TestClient(app=_app(policy=policy)) as client:
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

    assert accepted.status_code == 202
    assert ignored.status_code == 202
    assert policy.partials == [("create_document", {"title": "NDA"}, "s1")]


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
