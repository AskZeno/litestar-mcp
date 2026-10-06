"""An exception no handler maps never answers with its text.

Contract:
    Given a tool, resource, prompt, task, or JSON-RPC method raises an
    exception that is neither an ``MCPToolErrorResult`` nor a Litestar
    ``HTTPException``
    When the MCP call answers
    Then the caller reads only ``The tool failed. Reference: <reference>``,
    and one ERROR record carries the exception under that reference

Invariants:
    - A Litestar ``HTTPException`` keeps its client-facing detail.
    - ``MCPConfig.tool_exception_result`` renders a tool's content and
      receives the logged reference.
"""

import asyncio
import logging
import re
from typing import Any

import pytest
from litestar import Litestar, Request, get, post
from litestar.exceptions import PermissionDeniedException
from litestar.testing import TestClient

from litestar_mcp import AsyncioTaskBackend, LitestarMCP, MCPConfig, MCPTaskStore, TaskInvocation
from litestar_mcp.jsonrpc import INTERNAL_ERROR, JSONRPCRequest, JSONRPCRouter
from litestar_mcp.services.handler import RequestContext

pytestmark = pytest.mark.unit

SECRET = "could not connect to db-primary.internal:5432 (relation secret_table, id 42)"
FAILURE = re.compile(r"^The tool failed\. Reference: (?P<reference>[0-9a-f]{32})$")


class _InternalError(Exception):
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


def _reference_of(text: "str") -> "str":
    match = FAILURE.match(text)
    assert match is not None, text
    return match.group("reference")


def _assert_logged_once(caplog: "pytest.LogCaptureFixture", reference: "str") -> "logging.LogRecord":
    records: list[logging.LogRecord] = [
        record for record in caplog.records if getattr(record, "mcp_error_reference", None) == reference
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], _InternalError)
    assert reference in record.getMessage()
    return record


def test_tool_answers_unmapped_exception_with_its_logged_reference(caplog: "pytest.LogCaptureFixture") -> "None":
    @post("/tool", mcp_tool="tool", sync_to_thread=False)
    def tool() -> "dict[str, str]":
        raise _InternalError(SECRET)

    caplog.set_level(logging.ERROR)
    with TestClient(app=Litestar(route_handlers=[tool], plugins=[LitestarMCP()], logging_config=None)) as client:
        resp = _rpc(client, "tools/call", {"name": "tool", "arguments": {}})

    result = resp["result"]
    assert result["isError"] is True
    [block] = result["content"]
    reference = _reference_of(block["text"])
    assert result["_meta"]["dev.litestar/retryable"] is True
    assert SECRET not in str(resp)
    _assert_logged_once(caplog, reference)


def test_tool_http_exception_keeps_its_detail() -> "None":
    @post("/tool", mcp_tool="tool", sync_to_thread=False)
    def tool() -> "dict[str, str]":
        refusal = "workspace is read-only"
        raise PermissionDeniedException(refusal)

    with TestClient(app=Litestar(route_handlers=[tool], plugins=[LitestarMCP()])) as client:
        resp = _rpc(client, "tools/call", {"name": "tool", "arguments": {}})

    assert resp["result"]["isError"] is True
    assert "workspace is read-only" in resp["result"]["content"][0]["text"]
    assert resp["result"]["_meta"]["dev.litestar/retryable"] is False


def test_tool_exception_renderer_receives_the_logged_reference(caplog: "pytest.LogCaptureFixture") -> "None":
    seen: list[tuple[str, Exception, str, bool]] = []

    def render(
        tool_name: "str", exception: "Exception", reference: "str", request: "Request[Any, Any, Any] | None"
    ) -> "Any":
        seen.append((tool_name, exception, reference, request is not None))
        return {"failure": reference}

    @post("/tool", mcp_tool="tool", sync_to_thread=False)
    def tool() -> "dict[str, str]":
        raise _InternalError(SECRET)

    caplog.set_level(logging.ERROR)
    plugin = LitestarMCP(MCPConfig(tool_exception_result=render))
    with TestClient(app=Litestar(route_handlers=[tool], plugins=[plugin], logging_config=None)) as client:
        resp = _rpc(client, "tools/call", {"name": "tool", "arguments": {}})

    [(tool_name, exception, reference, has_request)] = seen
    assert (tool_name, type(exception), has_request) == ("tool", _InternalError, True)
    assert resp["result"]["content"][0]["text"] == f'{{"failure":"{reference}"}}'
    _assert_logged_once(caplog, reference)


def test_resource_answers_unmapped_exception_with_its_logged_reference(caplog: "pytest.LogCaptureFixture") -> "None":
    @get("/report", mcp_resource="report", sync_to_thread=False)
    def report() -> "dict[str, str]":
        raise _InternalError(SECRET)

    caplog.set_level(logging.ERROR)
    with TestClient(app=Litestar(route_handlers=[report], plugins=[LitestarMCP()], logging_config=None)) as client:
        resp = _rpc(client, "resources/read", {"uri": "litestar://report"})

    error = resp["error"]
    assert (error["code"], error["message"], error["data"]["statusCode"]) == (
        INTERNAL_ERROR,
        "Resource read failed",
        500,
    )
    [block] = error["data"]["content"]
    _assert_logged_once(caplog, _reference_of(block["text"]))
    assert SECRET not in str(resp)


def test_resource_http_exception_keeps_its_detail() -> "None":
    @get("/report", mcp_resource="report", sync_to_thread=False)
    def report() -> "dict[str, str]":
        refusal = "report is private"
        raise PermissionDeniedException(refusal)

    with TestClient(app=Litestar(route_handlers=[report], plugins=[LitestarMCP()])) as client:
        resp = _rpc(client, "resources/read", {"uri": "litestar://report"})

    assert resp["error"]["message"] == "Resource read failed"
    assert "report is private" in str(resp["error"]["data"])


def test_prompt_answers_unmapped_exception_with_its_logged_reference(caplog: "pytest.LogCaptureFixture") -> "None":
    @get("/brief", mcp_prompt="brief", sync_to_thread=False)
    def brief() -> "str":
        raise _InternalError(SECRET)

    caplog.set_level(logging.ERROR)
    with TestClient(app=Litestar(route_handlers=[brief], plugins=[LitestarMCP()], logging_config=None)) as client:
        resp = _rpc(client, "prompts/get", {"name": "brief"})

    error = resp["error"]
    assert (error["code"], error["message"], error["data"]["statusCode"]) == (
        INTERNAL_ERROR,
        "Prompt execution failed",
        500,
    )
    [block] = error["data"]["content"]
    _assert_logged_once(caplog, _reference_of(block["text"]))
    assert SECRET not in str(resp)


def test_prompt_http_exception_keeps_its_detail() -> "None":
    @get("/brief", mcp_prompt="brief", sync_to_thread=False)
    def brief() -> "str":
        refusal = "brief is private"
        raise PermissionDeniedException(refusal)

    with TestClient(app=Litestar(route_handlers=[brief], plugins=[LitestarMCP()], logging_config=None)) as client:
        resp = _rpc(client, "prompts/get", {"name": "brief"})

    assert resp["error"]["data"] == {
        "error": "PermissionDeniedException",
        "detail": "403: brief is private",
        "statusCode": 403,
    }


@pytest.mark.asyncio
async def test_task_backend_failure_reports_only_its_logged_reference(caplog: "pytest.LogCaptureFixture") -> "None":
    store = MCPTaskStore()
    backend = AsyncioTaskBackend()
    backend.bind(store)
    record = await store.create(owner_id=None)

    async def run_tool(_responses: "Any", _state: "Any") -> "dict[str, Any]":
        raise _InternalError(SECRET)

    caplog.set_level(logging.ERROR)
    await backend.start(record, TaskInvocation(record.task_id, "tool", {}, None, run_tool))
    await asyncio.gather(*backend._runners.values(), return_exceptions=True)
    failed = await store.load(record.task_id)

    assert failed.status == "failed"
    reference = _reference_of(failed.status_message or "")
    assert failed.error is not None
    assert failed.error.message == failed.status_message
    assert SECRET not in str(failed.to_dict())
    _assert_logged_once(caplog, reference)


@pytest.mark.asyncio
async def test_jsonrpc_blanket_catch_answers_only_its_logged_reference(caplog: "pytest.LogCaptureFixture") -> "None":
    router = JSONRPCRouter()

    async def explode(_params: "dict[str, Any]", _context: "Any") -> "dict[str, Any]":
        raise _InternalError(SECRET)

    router.register("test/explode", explode)
    caplog.set_level(logging.ERROR)
    resp = await router.dispatch(
        JSONRPCRequest(jsonrpc="2.0", method="test/explode", id=1),
        RequestContext(client_id="test", owner_id=None),
    )

    assert resp is not None
    assert (resp["error"]["code"], resp["error"]["message"]) == (INTERNAL_ERROR, "Internal error")
    reference = resp["error"]["data"]["reference"]
    assert SECRET not in str(resp)
    _assert_logged_once(caplog, reference)
