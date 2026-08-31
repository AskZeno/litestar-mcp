"""Client JSON-RPC notifications over Streamable HTTP POST."""

from typing import Any

import pytest
from litestar import Litestar, get
from litestar.testing import TestClient

from litestar_mcp import LitestarMCP, MCPConfig
from litestar_mcp.services.handler import MCPRequestContext

pytestmark = pytest.mark.unit


def _app(
    *,
    handlers: dict[str, Any] | None = None,
    extensions: dict[str, dict[str, Any]] | None = None,
) -> Litestar:
    @get("/z", mcp_tool="z_tool", sync_to_thread=False)
    def z_tool() -> dict[str, str]:
        return {"name": "z"}

    return Litestar(
        route_handlers=[z_tool],
        plugins=[
            LitestarMCP(
                MCPConfig(
                    notification_handlers=handlers or {},
                    extensions=extensions or {},
                )
            )
        ],
    )


def test_unknown_client_notification_is_accepted() -> None:
    with TestClient(app=_app()) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert response.status_code == 202
    assert response.content == b""
    assert response.headers["mcp-protocol-version"] == "2026-07-28"


def test_registered_notification_handler_runs_and_returns_202() -> None:
    received: list[tuple[str, dict[str, Any]]] = []

    async def on_partial(params: dict[str, Any], context: MCPRequestContext) -> None:
        del context
        received.append((str(params.get("name")), dict(params)))

    with TestClient(app=_app(handlers={"notifications/tools/input_partial": on_partial})) as client:
        response = client.post(
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

    assert response.status_code == 202
    assert response.content == b""
    assert len(received) == 1
    assert received[0][0] == "create_document"
    assert received[0][1]["streamId"] == "s1"
    assert received[0][1]["arguments"] == {"title": "NDA"}


def test_notification_handler_error_is_http_400_without_jsonrpc_id() -> None:
    async def boom(params: dict[str, Any], context: MCPRequestContext) -> None:
        del params, context
        raise RuntimeError("nope")

    with TestClient(app=_app(handlers={"notifications/tools/input_cancelled": boom})) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/tools/input_cancelled",
                "params": {"name": "create_document", "streamId": "s1"},
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32603


def test_unofficial_extensions_are_advertised_on_discover() -> None:
    config = MCPConfig(extensions={"law.zeno/streamable-tools": {}})

    @get("/z", mcp_tool="z_tool", sync_to_thread=False)
    def z_tool() -> dict[str, str]:
        return {"name": "z"}

    app = Litestar(route_handlers=[z_tool], plugins=[LitestarMCP(config)])
    with TestClient(app=app) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
                    }
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "server/discover",
            },
        )

    assert response.status_code == 200
    extensions = response.json()["result"]["capabilities"]["extensions"]
    assert extensions["law.zeno/streamable-tools"] == {}
