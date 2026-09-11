"""Typed hints share the existing decorator metadata and route opt discovery."""

from copy import deepcopy
from typing import Any

import pytest
from litestar import Litestar, get
from litestar.testing import TestClient

from litestar_mcp import MCP, LitestarMCP, MCPConfig, mcp_tool
from litestar_mcp.config import MCPOptKeys
from litestar_mcp.utils import get_mcp_metadata


def _list_tools(client: TestClient[Any]) -> list[dict[str, Any]]:
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools: list[dict[str, Any]] = response.json()["result"]["tools"]
    return tools


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("outer_decorator", [False, True])
def test_decorator_hints_preserve_boolean_values(value: bool, outer_decorator: bool) -> None:
    expected = {
        "readOnlyHint": value,
        "destructiveHint": value,
        "idempotentHint": value,
        "openWorldHint": value,
    }

    def tool() -> str:
        return "ok"

    mark = mcp_tool(
        "tool",
        read_only_hint=value,
        destructive_hint=value,
        idempotent_hint=value,
        open_world_hint=value,
    )
    route = get("/tool", sync_to_thread=False)
    handler = mark(route(tool)) if outer_decorator else route(mark(tool))
    metadata = get_mcp_metadata(handler)
    assert metadata is not None
    assert metadata["annotations"] == expected
    assert "read_only_hint" not in metadata  # No parallel hint metadata store.
    app = Litestar(route_handlers=[handler], plugins=[LitestarMCP()])
    with TestClient(app) as client:
        assert _list_tools(client)[0]["annotations"] == expected


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("declaration", ["route_kwargs", "route_opt", "standalone"])
def test_route_hints_preserve_boolean_values(value: bool, declaration: str) -> None:
    opt: dict[str, Any] = {
        "mcp_tool": "tool",
        "mcp_read_only_hint": value,
        "mcp_destructive_hint": value,
        "mcp_idempotent_hint": value,
        "mcp_open_world_hint": value,
    }

    def tool() -> str:
        return "ok"

    assert get_mcp_metadata(tool) is None
    if declaration == "standalone":
        standalone = MCP("hints")
        standalone.tool("tool", opt=opt, sync_to_thread=False)(tool)
        app = standalone.app
    else:
        route = (
            get("/tool", sync_to_thread=False, **opt)
            if declaration == "route_kwargs"
            else get("/tool", sync_to_thread=False, opt=opt)
        )
        app = Litestar(route_handlers=[route(tool)], plugins=[LitestarMCP()])
    with TestClient(app) as client:
        assert _list_tools(client)[0]["annotations"] == {
            "readOnlyHint": value,
            "destructiveHint": value,
            "idempotentHint": value,
            "openWorldHint": value,
        }


def test_hint_precedence_does_not_mutate_existing_annotations() -> None:
    annotations = {"title": "Inventory", "readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
    original = deepcopy(annotations)

    @get("/tool", mcp_read_only_hint=False, mcp_open_world_hint=False, sync_to_thread=False)
    @mcp_tool(
        "tool",
        annotations=annotations,
        scopes=["inventory:read"],
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
    )
    def tool() -> str:
        return "ok"

    metadata = deepcopy(get_mcp_metadata(tool))
    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()])
    with TestClient(app) as client:
        for _ in range(2):
            assert _list_tools(client)[0]["annotations"] == {
                "title": "Inventory",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
                "scopes": ["inventory:read"],
            }
    assert annotations == original
    assert get_mcp_metadata(tool) == metadata


def test_hint_opt_keys_are_remappable() -> None:
    keys = MCPOptKeys(
        read_only_hint="x_read",
        destructive_hint="x_destructive",
        idempotent_hint="x_idempotent",
        open_world_hint="x_open",
    )

    @get(
        "/tool",
        mcp_tool="tool",
        opt={"x_read": False, "x_destructive": False, "x_idempotent": False, "x_open": False},
        mcp_read_only_hint=True,
        mcp_destructive_hint=True,
        mcp_idempotent_hint=True,
        mcp_open_world_hint=True,
        sync_to_thread=False,
    )
    def tool() -> str:
        return "ok"

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP(MCPConfig(opt_keys=keys))])
    with TestClient(app) as client:
        assert _list_tools(client)[0]["annotations"] == {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }


@pytest.mark.parametrize("value", [None, "false", 0, 1, {}, []])
def test_non_boolean_opt_hints_are_not_coerced(value: Any) -> None:
    @get(
        "/tool",
        mcp_read_only_hint=value,
        mcp_destructive_hint=value,
        mcp_idempotent_hint=value,
        mcp_open_world_hint=value,
        sync_to_thread=False,
    )
    @mcp_tool("tool", read_only_hint=False)
    def tool() -> str:
        return "ok"

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()])
    with TestClient(app) as client:
        assert _list_tools(client)[0]["annotations"] == {"readOnlyHint": False}


def test_undeclared_hints_stay_absent() -> None:
    @get("/tool", sync_to_thread=False)
    @mcp_tool("tool")
    def tool() -> str:
        return "ok"

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()])
    with TestClient(app) as client:
        assert "annotations" not in _list_tools(client)[0]
