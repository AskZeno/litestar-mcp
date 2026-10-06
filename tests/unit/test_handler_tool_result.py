"""An exception handler may answer with a complete ``MCPToolResult``.

Contract:
    Given an exception handler returns an ``MCPToolResult`` (bare, or as the
        content of a Litestar ``Response`` that carries its status)
    When a tool raises the exception it maps
    Then the ``tools/call`` result is that tool result as-is: its content,
        ``structuredContent`` and result-level ``_meta`` reach the caller, and
        ``isError`` follows the response status
    When a resource read or prompt raises it
    Then the JSON-RPC ``error.data`` carries the same result beside
        ``statusCode``

Invariants:
    - The handled response still drives the dispatch scope's ``send``
      lifecycle with its status, so request cleanup hooks fire.
    - ``dev.litestar/retryable`` follows the status unless the result
      declares it.
    - ``after_tool_call`` sees the failure as an ``MCPToolErrorResult``
      whose content is the tool result.
"""

from typing import Any

import pytest
from litestar import Litestar, Request, get, post
from litestar.response import Response
from litestar.testing import TestClient

from litestar_mcp import LitestarMCP, MCPConfig, MCPToolResult
from litestar_mcp.executor import MCPToolErrorResult
from litestar_mcp.jsonrpc import INTERNAL_ERROR

pytestmark = pytest.mark.unit

PROBLEM = {"type": "urn:example:stale", "status": 409}


class _RefusedError(Exception):
    """A refusal its exception handler answers with a tool result."""


def _tool_result(*, retryable: "bool | None" = None) -> "MCPToolResult":
    meta: dict[str, Any] = {"example.com/problem": PROBLEM}
    if retryable is not None:
        meta["dev.litestar/retryable"] = retryable
    return MCPToolResult(
        content=[{"type": "text", "text": "Read the draft again."}],
        structured_content={"legacy": True},
        is_error=True,
        meta=meta,
    )


def _with_status(_request: "Request[Any, Any, Any]", _exc: "_RefusedError") -> "Response[MCPToolResult]":
    return Response(_tool_result(), status_code=409)


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


def _call(app: "Litestar") -> "dict[str, Any]":
    with TestClient(app=app) as client:
        return _rpc(client, "tools/call", {"name": "probe", "arguments": {}})["result"]  # type: ignore[no-any-return]


def test_handled_tool_result_reaches_the_caller_whole() -> "None":
    @post("/probe", mcp_tool="probe", exception_handlers={_RefusedError: _with_status}, sync_to_thread=False)
    def probe() -> "None":
        raise _RefusedError

    result = _call(Litestar(route_handlers=[probe], plugins=[LitestarMCP()]))

    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "Read the draft again."}]
    assert result["structuredContent"] == {"legacy": True}
    assert result["_meta"]["example.com/problem"] == PROBLEM
    assert result["_meta"]["dev.litestar/retryable"] is True


def test_a_declared_retryability_wins_over_the_status() -> "None":
    def terminal(_request: "Request[Any, Any, Any]", _exc: "_RefusedError") -> "Response[MCPToolResult]":
        return Response(_tool_result(retryable=False), status_code=409)

    @post("/probe", mcp_tool="probe", exception_handlers={_RefusedError: terminal}, sync_to_thread=False)
    def probe() -> "None":
        raise _RefusedError

    result = _call(Litestar(route_handlers=[probe], plugins=[LitestarMCP()]))

    assert result["_meta"]["dev.litestar/retryable"] is False


def test_a_bare_tool_result_answers_as_a_server_error() -> "None":
    def bare(_request: "Request[Any, Any, Any]", _exc: "_RefusedError") -> "MCPToolResult":
        return _tool_result()

    # Litestar types a handler as returning a Response; the executor also takes a bare result.
    @post("/probe", mcp_tool="probe", exception_handlers={_RefusedError: bare}, sync_to_thread=False)  # type: ignore[dict-item]
    def probe() -> "None":
        raise _RefusedError

    seen: list[tuple[int, Any]] = []

    def after_tool_call(*_args: "Any", exception: "Exception | None", **_kwargs: "Any") -> "None":
        assert isinstance(exception, MCPToolErrorResult)
        seen.append((exception.status_code, exception.content))

    app = Litestar(route_handlers=[probe], plugins=[LitestarMCP(MCPConfig(after_tool_call=after_tool_call))])
    result = _call(app)

    assert result["isError"] is True
    assert result["_meta"]["example.com/problem"] == PROBLEM
    assert seen == [(500, _tool_result())]


def test_a_successful_status_recovers_the_call() -> "None":
    def recovered(_request: "Request[Any, Any, Any]", _exc: "_RefusedError") -> "Response[MCPToolResult]":
        return Response(MCPToolResult(content="recovered", meta={"example.com/note": 1}), status_code=200)

    @post("/probe", mcp_tool="probe", exception_handlers={_RefusedError: recovered}, sync_to_thread=False)
    def probe() -> "None":
        raise _RefusedError

    result = _call(Litestar(route_handlers=[probe], plugins=[LitestarMCP()]))

    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": "recovered"}]
    assert result["_meta"]["example.com/note"] == 1


def test_the_handled_status_drives_the_send_lifecycle() -> "None":
    seen: list[tuple[str, Any]] = []

    async def before_send(message: "dict[str, Any]", scope: "dict[str, Any]") -> "None":
        if scope.get("litestar_mcp.internal_dispatch"):
            seen.append((str(message["type"]), message.get("status")))

    @post("/probe", mcp_tool="probe", exception_handlers={_RefusedError: _with_status}, sync_to_thread=False)
    def probe() -> "None":
        raise _RefusedError

    _call(Litestar(route_handlers=[probe], plugins=[LitestarMCP()], before_send=[before_send]))  # type: ignore[list-item]

    assert seen == [("http.response.start", 409), ("http.response.body", None)]


def test_resource_error_data_carries_the_tool_result() -> "None":
    @get("/report", mcp_resource="report", exception_handlers={_RefusedError: _with_status}, sync_to_thread=False)
    def report() -> "dict[str, str]":
        raise _RefusedError

    with TestClient(app=Litestar(route_handlers=[report], plugins=[LitestarMCP()])) as client:
        error = _rpc(client, "resources/read", {"uri": "litestar://report"})["error"]

    assert (error["code"], error["message"]) == (INTERNAL_ERROR, "Resource read failed")
    assert error["data"] == {
        "statusCode": 409,
        "content": [{"type": "text", "text": "Read the draft again."}],
        "structuredContent": {"legacy": True},
        "_meta": {"example.com/problem": PROBLEM},
    }


def test_prompt_error_data_carries_the_tool_result() -> "None":
    @get("/brief", mcp_prompt="brief", exception_handlers={_RefusedError: _with_status}, sync_to_thread=False)
    def brief() -> "str":
        raise _RefusedError

    with TestClient(app=Litestar(route_handlers=[brief], plugins=[LitestarMCP()])) as client:
        error = _rpc(client, "prompts/get", {"name": "brief"})["error"]

    assert (error["code"], error["message"]) == (INTERNAL_ERROR, "Prompt execution failed")
    assert error["data"]["statusCode"] == 409
    assert error["data"]["_meta"] == {"example.com/problem": PROBLEM}
