"""MCP Apps extension tests: handshake and ui:// resource visibility."""

from typing import Any, cast

from litestar import Litestar, get
from litestar.testing import TestClient

from litestar_mcp import LitestarMCP, MCPAppsConfig, MCPConfig

PROTOCOL_VERSION = "2026-07-28"
APPS_EXTENSION = "io.modelcontextprotocol/apps"


def _rpc(
    client: TestClient[Any],
    method: str,
    params: dict[str, Any] | None = None,
    *,
    apps_capable: bool = False,
) -> dict[str, Any]:
    request_params = dict(params or {})
    capabilities: dict[str, Any] = {}
    if apps_capable:
        capabilities["extensions"] = {APPS_EXTENSION: {}}
    request_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": capabilities,
        "io.modelcontextprotocol/clientInfo": {"name": "apps-tests", "version": "1"},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    return cast(
        "dict[str, Any]",
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": request_params},
            headers=headers,
        ).json(),
    )


def _make_app(*, apps: bool | MCPAppsConfig = False) -> Litestar:
    @get(
        "/panel",
        media_type="text/html",
        mcp_resource="review_panel",
        mcp_resource_uri="ui://review/panel",
        mcp_resource_mime_type="text/html",
        sync_to_thread=False,
    )
    def review_panel() -> str:
        return "<html><body>panel</body></html>"

    @get("/plain", mcp_resource="plain_doc", sync_to_thread=False)
    def plain_doc() -> dict[str, str]:
        return {"kind": "plain"}

    return Litestar(
        route_handlers=[review_panel, plain_doc],
        plugins=[LitestarMCP(MCPConfig(apps=apps))],
    )


def test_discovery_advertises_apps_extension_only_when_configured() -> None:
    with TestClient(app=_make_app(apps=True)) as client:
        enabled = _rpc(client, "server/discover")["result"]
    with TestClient(app=_make_app(apps=False)) as client:
        disabled = _rpc(client, "server/discover")["result"]

    assert enabled["capabilities"]["extensions"] == {APPS_EXTENSION: {}}
    assert "extensions" not in disabled["capabilities"]


def test_ui_resources_are_listed_only_for_capable_clients_of_an_enabled_server() -> None:
    with TestClient(app=_make_app(apps=True)) as client:
        capable = _rpc(client, "resources/list", apps_capable=True)["result"]["resources"]
        incapable = _rpc(client, "resources/list")["result"]["resources"]
    with TestClient(app=_make_app(apps=False)) as client:
        disabled = _rpc(client, "resources/list", apps_capable=True)["result"]["resources"]

    def uris(resources: list[dict[str, Any]]) -> set[str]:
        return {resource["uri"] for resource in resources}

    assert "ui://review/panel" in uris(capable)
    assert "ui://review/panel" not in uris(incapable)
    assert "ui://review/panel" not in uris(disabled)
    assert all("litestar://plain_doc" in uris(group) for group in (capable, incapable, disabled))


def test_ui_resource_read_is_gated_like_listing() -> None:
    with TestClient(app=_make_app(apps=True)) as client:
        readable = _rpc(client, "resources/read", {"uri": "ui://review/panel"}, apps_capable=True)
        hidden = _rpc(client, "resources/read", {"uri": "ui://review/panel"})
    with TestClient(app=_make_app(apps=False)) as client:
        disabled = _rpc(client, "resources/read", {"uri": "ui://review/panel"}, apps_capable=True)

    contents = readable["result"]["contents"]
    assert contents[0]["uri"] == "ui://review/panel"
    assert contents[0]["mimeType"] == "text/html"
    assert "panel" in contents[0]["text"]
    assert "error" in hidden
    assert "error" in disabled


def test_declared_ui_resource_resolves_inert_when_apps_disabled() -> None:
    with TestClient(app=_make_app(apps=False)) as client:
        listed = _rpc(client, "resources/list")["result"]["resources"]
        read = _rpc(client, "resources/read", {"uri": "litestar://plain_doc"})

    assert {resource["uri"] for resource in listed} == {"litestar://openapi", "litestar://plain_doc"}
    assert read["result"]["contents"][0]["uri"] == "litestar://plain_doc"
