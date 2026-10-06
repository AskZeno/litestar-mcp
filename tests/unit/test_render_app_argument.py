"""Rendering a dispatch's response passes no deprecated ``app`` argument.

Litestar deprecates ``Response.to_asgi_response(app=...)`` (the request
carries the app); a non-``None`` value warns, which strict-warning suites
turn into errors. The executor renders handled exceptions, broken exception
renderers, unhandled-exception cleanup, and specification results.
"""

import warnings
from typing import Any

import pytest
from litestar import Litestar, Request, post
from litestar.response import Response
from litestar.testing import TestClient
from pydantic import BaseModel, Field

from litestar_mcp import LitestarMCP

pytestmark = pytest.mark.unit


class _MappedError(Exception):
    """An exception a layered exception handler maps."""


class _UnmappedError(Exception):
    """An exception no handler maps."""


def _rpc(client: "TestClient[Any]", method: "str", params: "dict[str, Any]") -> "dict[str, Any]":
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}},
        },
    )
    headers = {"Mcp-Session-Id": init.headers.get("mcp-session-id", "")}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return client.post("/mcp", json=body, headers=headers).json()  # type: ignore[no-any-return]


class _SpecResult(BaseModel):
    """Structural CallToolResult model, without a dependency on a spec package."""

    content: list[dict[str, Any]] = Field(default_factory=list)
    structured_content: dict[str, Any] | None = Field(default=None, alias="structuredContent")
    is_error: bool = Field(default=False, alias="isError")


def _mapped(_request: "Request[Any, Any, Any]", exc: "_MappedError") -> "Response[Any]":
    return Response(content={"mapped": str(exc)}, status_code=422)


def _broken_renderer(_request: "Request[Any, Any, Any]", _exc: "_MappedError") -> "Response[Any]":
    msg = "renderer broke"
    raise RuntimeError(msg)


def test_rendering_passes_no_deprecated_app_argument() -> "None":
    """Handled, unhandled, broken-renderer, and specification results render without ``app=``."""
    failure = "x"

    @post("/mapped", mcp_tool="mapped", exception_handlers={_MappedError: _mapped}, sync_to_thread=False)
    def mapped() -> "dict[str, str]":
        raise _MappedError(failure)

    @post("/broken", mcp_tool="broken", exception_handlers={_MappedError: _broken_renderer}, sync_to_thread=False)
    def broken() -> "dict[str, str]":
        raise _MappedError(failure)

    @post("/unmapped", mcp_tool="unmapped", sync_to_thread=False)
    def unmapped() -> "dict[str, str]":
        raise _UnmappedError(failure)

    @post("/spec", mcp_tool="spec", sync_to_thread=False)
    def spec() -> "_SpecResult":
        return _SpecResult(content=[{"type": "text", "text": "ok"}])

    app = Litestar(route_handlers=[mapped, broken, unmapped, spec], plugins=[LitestarMCP()], logging_config=None)
    with TestClient(app=app) as client, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        results = {
            name: _rpc(client, "tools/call", {"name": name, "arguments": {}})
            for name in ("mapped", "broken", "unmapped", "spec")
        }

    assert [results[name]["result"]["isError"] for name in ("mapped", "broken", "unmapped", "spec")] == [
        True,
        True,
        True,
        False,
    ]
    assert [str(w.message) for w in caught if "'app'" in str(w.message) or "request.app" in str(w.message)] == []
