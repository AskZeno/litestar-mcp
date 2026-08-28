"""MCP Apps extension (SEP-1865): io.modelcontextprotocol/ui server contract."""

import builtins
from typing import Any, cast

import pytest
from litestar import Litestar, get
from litestar.testing import TestClient

from litestar_mcp import UI_EXTENSION, UI_MIME_TYPE, LitestarMCP, MCPAppsConfig, MCPConfig
from litestar_mcp.utils import mcp_resource, mcp_tool

PROTOCOL_VERSION = "2026-07-28"


def _rpc(
    client: TestClient[Any],
    method: str,
    params: dict[str, Any] | None = None,
    *,
    ui_mime_types: list[str] | None = None,
    declare_without_settings: bool = False,
) -> dict[str, Any]:
    request_params = dict(params or {})
    capabilities: dict[str, Any] = {}
    if declare_without_settings:
        capabilities["extensions"] = {UI_EXTENSION: {}}
    elif ui_mime_types is not None:
        capabilities["extensions"] = {UI_EXTENSION: {"mimeTypes": ui_mime_types}}
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
    name_field = {"tools/call": "name", "resources/read": "uri"}.get(method)
    if name_field is not None:
        headers["Mcp-Name"] = str(request_params.get(name_field, ""))
    return cast(
        "dict[str, Any]",
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": request_params},
            headers=headers,
        ).json(),
    )


def _make_app(*, apps: bool | MCPAppsConfig = True) -> Litestar:
    @get(
        "/panel",
        media_type="text/html",
        mcp_resource_uri="ui://review/panel",
        mcp_resource_mime_type=UI_MIME_TYPE,
        sync_to_thread=False,
    )
    @mcp_resource(
        "review_panel",
        ui={"csp": {"connectDomains": ["https://api.example.com"]}, "prefersBorder": True},
    )
    def review_panel() -> str:
        return "<html><body>panel</body></html>"

    @get("/review", sync_to_thread=False)
    @mcp_tool(name="review_document", ui_resource_uri="ui://review/panel", ui_visibility=["model", "app"])
    async def review_document() -> dict[str, str]:
        return {"status": "reviewed"}

    @get("/plain", mcp_resource="plain_doc", sync_to_thread=False)
    def plain_doc() -> dict[str, str]:
        return {"kind": "plain"}

    return Litestar(
        route_handlers=[review_panel, review_document, plain_doc],
        plugins=[LitestarMCP(MCPConfig(apps=apps))],
    )


def test_discover_advertises_the_official_identifier_with_mime_types() -> None:
    with TestClient(app=_make_app(apps=True)) as client:
        enabled = _rpc(client, "server/discover")["result"]
    with TestClient(app=_make_app(apps=False)) as client:
        disabled = _rpc(client, "server/discover")["result"]

    assert enabled["capabilities"]["extensions"] == {UI_EXTENSION: {"mimeTypes": [UI_MIME_TYPE]}}
    assert "extensions" not in disabled["capabilities"]
    assert "io.modelcontextprotocol/apps" not in str(enabled)


def test_tool_meta_ui_rides_only_to_mime_capable_clients() -> None:
    with TestClient(app=_make_app()) as client:
        capable = _rpc(client, "tools/list", ui_mime_types=[UI_MIME_TYPE])["result"]["tools"]
        incapable = _rpc(client, "tools/list")["result"]["tools"]
        wrong_mime = _rpc(client, "tools/list", ui_mime_types=["text/plain"])["result"]["tools"]
        no_settings = _rpc(client, "tools/list", declare_without_settings=True)["result"]["tools"]

    def entry(tools: list[dict[str, Any]]) -> dict[str, Any]:
        return next(tool for tool in tools if tool["name"] == "review_document")

    assert entry(capable)["_meta"]["ui"] == {
        "resourceUri": "ui://review/panel",
        "visibility": ["model", "app"],
    }
    for degraded in (incapable, wrong_mime, no_settings):
        assert "_meta" not in entry(degraded)
        assert entry(degraded)["name"] == "review_document"


def test_ui_resources_visible_only_with_mime_intersection() -> None:
    with TestClient(app=_make_app()) as client:
        capable = {
            r["uri"]: r for r in _rpc(client, "resources/list", ui_mime_types=[UI_MIME_TYPE])["result"]["resources"]
        }
        incapable = {r["uri"] for r in _rpc(client, "resources/list")["result"]["resources"]}

    assert capable["ui://review/panel"]["mimeType"] == UI_MIME_TYPE
    assert "ui://review/panel" not in incapable
    assert "litestar://plain_doc" in incapable


def test_ui_resource_read_returns_profile_mime_and_meta_ui_passthrough() -> None:
    with TestClient(app=_make_app()) as client:
        readable = _rpc(client, "resources/read", {"uri": "ui://review/panel"}, ui_mime_types=[UI_MIME_TYPE])
        hidden = _rpc(client, "resources/read", {"uri": "ui://review/panel"})

    content = readable["result"]["contents"][0]
    assert content["uri"] == "ui://review/panel"
    assert "panel" in content["text"]
    assert content["_meta"]["ui"] == {
        "csp": {"connectDomains": ["https://api.example.com"]},
        "prefersBorder": True,
    }
    assert "error" in hidden


def _assert_startup_value_error(app: Litestar, needle: str) -> None:
    """Startup failures surface wrapped in the lifespan's exception group."""

    group_type = getattr(builtins, "BaseExceptionGroup", None)

    def flatten(error: BaseException) -> list[BaseException]:
        if group_type is not None and isinstance(error, group_type):
            return [leaf for sub in error.exceptions for leaf in flatten(sub)]  # type: ignore[attr-defined]
        return [error]

    with pytest.raises(BaseException) as excinfo, TestClient(app=app):
        pass
    leaves = flatten(excinfo.value)
    assert any(isinstance(leaf, ValueError) and needle in str(leaf) for leaf in leaves), leaves


def test_startup_rejects_a_dangling_ui_link() -> None:
    @get("/broken", sync_to_thread=False)
    @mcp_tool(name="broken_tool", ui_resource_uri="ui://missing/panel")
    async def broken_tool() -> dict[str, str]:
        return {}

    app = Litestar(route_handlers=[broken_tool], plugins=[LitestarMCP(MCPConfig(apps=True))])
    _assert_startup_value_error(app, "links ui resource")


def test_startup_rejects_a_ui_resource_outside_the_profile() -> None:
    @get(
        "/bad-panel",
        mcp_resource="bad_panel",
        mcp_resource_uri="ui://bad/panel",
        mcp_resource_mime_type="text/plain",
        sync_to_thread=False,
    )
    def bad_panel() -> str:
        return "nope"

    app = Litestar(route_handlers=[bad_panel], plugins=[LitestarMCP(MCPConfig(apps=True))])
    _assert_startup_value_error(app, "SEP-1865 requires")


def test_undeclared_ui_resource_defaults_to_the_profile_mime() -> None:
    @get("/auto-panel", mcp_resource="auto_panel", mcp_resource_uri="ui://auto/panel", sync_to_thread=False)
    def auto_panel() -> str:
        return "<html></html>"

    app = Litestar(route_handlers=[auto_panel], plugins=[LitestarMCP(MCPConfig(apps=True))])
    with TestClient(app=app) as client:
        listed = {
            r["uri"]: r for r in _rpc(client, "resources/list", ui_mime_types=[UI_MIME_TYPE])["result"]["resources"]
        }

    assert listed["ui://auto/panel"]["mimeType"] == UI_MIME_TYPE


def test_declared_ui_surface_resolves_inert_when_apps_disabled() -> None:
    with TestClient(app=_make_app(apps=False)) as client:
        tools = _rpc(client, "tools/list", ui_mime_types=[UI_MIME_TYPE])["result"]["tools"]
        listed = {r["uri"] for r in _rpc(client, "resources/list", ui_mime_types=[UI_MIME_TYPE])["result"]["resources"]}

    review = next(tool for tool in tools if tool["name"] == "review_document")
    assert "_meta" not in review
    assert "ui://review/panel" not in listed
