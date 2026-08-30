"""Red-phase tests for ``exception_handlers`` routing in the MCP executor (#41).

These pin the contract that ``execute_tool`` walks ``handler.resolve_exception_handlers()``
MRO-style, matching Litestar's own HTTP semantics: a matching handler returning
a ``Response`` with ``status_code < 400`` recovers ``is_error`` to ``False``;
status ``>= 400`` maps to ``is_error=True``. Unhandled exceptions fall through
to the blanket JSON-RPC catch in ``routes.execute_tool_call``.
"""

from typing import Any

import pytest
from litestar import Controller, Litestar, Request, Router, post
from litestar.response import Response
from litestar.testing import TestClient

from litestar_mcp import LitestarMCP

pytestmark = pytest.mark.unit


class DomainError(Exception):
    """Custom exception for exception-handler dispatch."""


class DomainChildError(DomainError):
    """Sub-type used for MRO-matching tests."""


def _ensure_session(client: "TestClient[Any]") -> "str":
    sid = getattr(client, "_mcp_session", None)
    if sid is not None:
        return str(sid)
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}},
        },
    )
    sid_val = init.headers.get("mcp-session-id", "")
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Mcp-Session-Id": sid_val},
    )
    client._mcp_session = sid_val  # type: ignore[attr-defined]
    return str(sid_val)


def _call_tool(client: "TestClient[Any]", name: "str") -> "dict[str, Any]":
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}
    sid = _ensure_session(client)
    headers = {"Mcp-Session-Id": sid} if sid else {}
    return client.post("/mcp", json=body, headers=headers).json()  # type: ignore[no-any-return]


def _handler_returns_4xx(_request: "Request[Any, Any, Any]", exc: "DomainError") -> "Response[Any]":
    return Response(content={"domain_error": str(exc)}, status_code=422)


def _handler_returns_2xx(_request: "Request[Any, Any, Any]", exc: "DomainError") -> "Response[Any]":
    return Response(content={"recovered": True, "message": str(exc)}, status_code=200)


def test_exception_handler_error_response_becomes_is_error_true() -> "None":
    nope = "nope"

    @post(
        "/x",
        exception_handlers={DomainError: _handler_returns_4xx},
        mcp_tool="x",
        sync_to_thread=False,
    )
    def tool() -> "dict[str, str]":
        raise DomainError(nope)

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        resp = _call_tool(client, "x")

    assert resp["result"]["isError"] is True
    assert "domain_error" in resp["result"]["content"][0]["text"]
    assert "nope" in resp["result"]["content"][0]["text"]


def test_handled_exception_response_runs_inner_before_send_hooks() -> "None":
    seen: list[str] = []

    async def before_send(message: "dict[str, Any]", scope: "dict[str, Any]") -> "None":
        if scope.get("litestar_mcp.internal_dispatch"):
            seen.append(str(message["type"]))

    cleanup = "cleanup"

    @post(
        "/x",
        exception_handlers={DomainError: _handler_returns_4xx},
        mcp_tool="x",
        sync_to_thread=False,
    )
    def tool() -> "dict[str, str]":
        raise DomainError(cleanup)

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()], before_send=[before_send])  # type: ignore[list-item]
    with TestClient(app=app) as client:
        resp = _call_tool(client, "x")

    assert resp["result"]["isError"] is True
    assert seen == ["http.response.start", "http.response.body"]


def test_unhandled_exception_still_runs_inner_before_send_hooks() -> "None":
    """No matching layered exception handler: the error propagates to the
    JSON-RPC layer, but the dispatch scope must still see a terminal ASGI
    response so ``before_send``-keyed cleanup (advanced-alchemy session
    close, transaction teardown) releases request-scoped resources.
    """
    seen: list[str] = []

    async def before_send(message: "dict[str, Any]", scope: "dict[str, Any]") -> "None":
        if scope.get("litestar_mcp.internal_dispatch"):
            seen.append(str(message["type"]))

    leak_check = "leak check"

    @post("/x", mcp_tool="x", sync_to_thread=False)
    def tool() -> "dict[str, str]":
        raise DomainError(leak_check)

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()], before_send=[before_send])  # type: ignore[list-item]
    with TestClient(app=app) as client:
        resp = _call_tool(client, "x")

    assert resp["result"]["isError"] is True
    assert "leak check" in resp["result"]["content"][0]["text"]
    assert seen == ["http.response.start", "http.response.body"]


def test_exception_handler_2xx_response_recovers_to_is_error_false() -> "None":
    soft = "soft fail"

    @post(
        "/x",
        exception_handlers={DomainError: _handler_returns_2xx},
        mcp_tool="x",
        sync_to_thread=False,
    )
    def tool() -> "dict[str, str]":
        raise DomainError(soft)

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        resp = _call_tool(client, "x")

    assert resp["result"]["isError"] is False
    assert "recovered" in resp["result"]["content"][0]["text"]


def test_exception_handler_mro_matches_subclass() -> "None":
    """Registering a handler on ``DomainError`` must catch its subclass."""

    child_msg = "child raised"

    @post(
        "/x",
        exception_handlers={DomainError: _handler_returns_4xx},
        mcp_tool="x",
        sync_to_thread=False,
    )
    def tool() -> "dict[str, str]":
        raise DomainChildError(child_msg)

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        resp = _call_tool(client, "x")

    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert "domain_error" in text, "custom exception handler did not produce its Response"
    assert "child raised" in text


def test_unhandled_exception_falls_through_to_blanket_catch() -> "None":
    """An exception type without a registered handler goes to the JSON-RPC blanket catch."""

    generic = "generic boom"

    @post("/x", mcp_tool="x", sync_to_thread=False)
    def tool() -> "dict[str, str]":
        raise RuntimeError(generic)

    app = Litestar(route_handlers=[tool], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        resp = _call_tool(client, "x")

    assert resp["result"]["isError"] is True
    assert "generic boom" in resp["result"]["content"][0]["text"]


@pytest.mark.parametrize("layer", ["app", "router", "controller", "route"])
def test_exception_handler_resolves_from_ownership_layer(layer: "str") -> "None":
    if layer == "route":

        @post(
            "/x",
            exception_handlers={DomainError: _handler_returns_4xx},
            mcp_tool="x",
            sync_to_thread=False,
        )
        def route_tool() -> "dict[str, str]":
            raise DomainError(layer)

        app = Litestar(route_handlers=[route_tool], plugins=[LitestarMCP()])

    elif layer == "controller":

        class Notes(Controller):
            path = "/notes"
            exception_handlers = {DomainError: _handler_returns_4xx}

            @post("/x", mcp_tool="x", sync_to_thread=False)
            def tool(self) -> "dict[str, str]":
                raise DomainError(layer)

        app = Litestar(route_handlers=[Notes], plugins=[LitestarMCP()])

    elif layer == "router":

        @post("/x", mcp_tool="x", sync_to_thread=False)
        def rt_tool() -> "dict[str, str]":
            raise DomainError(layer)

        router = Router(
            path="/api",
            route_handlers=[rt_tool],
            exception_handlers={DomainError: _handler_returns_4xx},
        )
        app = Litestar(route_handlers=[router], plugins=[LitestarMCP()])

    else:

        @post("/x", mcp_tool="x", sync_to_thread=False)
        def app_tool() -> "dict[str, str]":
            raise DomainError(layer)

        app = Litestar(
            route_handlers=[app_tool],
            plugins=[LitestarMCP()],
            exception_handlers={DomainError: _handler_returns_4xx},
        )

    with TestClient(app=app) as client:
        resp = _call_tool(client, "x")

    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert "domain_error" in text, f"custom exception handler did not fire for layer={layer}"
    assert layer in text


def test_tool_error_results_carry_the_retryability_meta() -> "None":
    """Terminal HTTP statuses mark the error result non-retryable; transient
    and repairable statuses stay retryable — clients stop parsing prose.
    """
    from litestar.exceptions import NotFoundException

    conflict = "still processing; wait and retry"
    missing = "no such document"

    @post("/gone", mcp_tool="gone", sync_to_thread=False)
    def gone() -> "dict[str, str]":
        raise NotFoundException(missing)

    @post("/busy", mcp_tool="busy", sync_to_thread=False)
    def busy() -> "dict[str, str]":
        from litestar.exceptions import HTTPException

        raise HTTPException(status_code=409, detail=conflict)

    app = Litestar(route_handlers=[gone, busy], plugins=[LitestarMCP()])
    with TestClient(app=app) as client:
        gone_result = _call_tool(client, "gone")["result"]
        busy_result = _call_tool(client, "busy")["result"]

    assert gone_result["isError"] is True
    assert gone_result["_meta"]["dev.litestar/retryable"] is False
    assert busy_result["isError"] is True
    assert busy_result["_meta"]["dev.litestar/retryable"] is True
