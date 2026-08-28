"""Request-scoped progress: SSE ordering, plain silence, and cancellation."""

import asyncio
import json
import time
from typing import Any, cast

import pytest
from litestar import Litestar, get
from litestar.response import ServerSentEventMessage
from litestar.testing import TestClient

from litestar_mcp import (
    LitestarMCP,
    MCPConfig,
    ProgressReporter,
    RequestNotificationStream,
    get_mcp_request_context,
    progress_params,
)
from litestar_mcp.routes import _request_event_stream
from litestar_mcp.utils import mcp_tool

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


def _request_parts(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    progress_token: str | int | None = None,
    tasks_capable: bool = False,
    accept: str = "application/json",
) -> tuple[dict[str, Any], dict[str, str]]:
    request_params = dict(params or {})
    capabilities: dict[str, Any] = {}
    if tasks_capable:
        capabilities["extensions"] = {TASKS_EXTENSION: {}}
    meta: dict[str, Any] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": capabilities,
        "io.modelcontextprotocol/clientInfo": {"name": "progress-tests", "version": "1"},
    }
    if progress_token is not None:
        meta["progressToken"] = progress_token
    request_params["_meta"] = meta
    headers = {
        "Accept": accept,
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    name_field = {"tools/call": "name", "tasks/get": "taskId"}.get(method)
    if name_field is not None:
        headers["Mcp-Name"] = str(request_params.get(name_field, ""))
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": request_params}
    return body, headers


def _rpc(
    client: TestClient[Any],
    method: str,
    params: dict[str, Any] | None = None,
    *,
    progress_token: str | int | None = None,
    tasks_capable: bool = False,
) -> dict[str, Any]:
    body, headers = _request_parts(
        method,
        params,
        progress_token=progress_token,
        tasks_capable=tasks_capable,
    )
    response = client.post("/mcp", json=body, headers=headers)
    assert response.headers["content-type"].startswith("application/json")
    return cast("dict[str, Any]", response.json())


def _sse_rpc(
    client: TestClient[Any],
    method: str,
    params: dict[str, Any],
    *,
    progress_token: str | int,
    tasks_capable: bool = False,
) -> tuple[Any, list[dict[str, Any]]]:
    body, headers = _request_parts(
        method,
        params,
        progress_token=progress_token,
        tasks_capable=tasks_capable,
        accept="text/event-stream",
    )
    response = client.post("/mcp", json=body, headers=headers)
    messages = [
        json.loads(line.partition("data: ")[2]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    return response, messages


def _make_app() -> tuple[Litestar, list[tuple[str, dict[str, Any]]]]:
    subscription_publications: list[tuple[str, dict[str, Any]]] = []

    @get("/crunch", sync_to_thread=False)
    @mcp_tool(name="crunch", task_support="optional")
    async def crunch(rows: int = 3) -> dict[str, int]:
        context = get_mcp_request_context()
        for index in range(rows):
            await context.progress.report(
                index + 1,
                total=rows,
                message=f"row {index + 1} of {rows}",
                meta={"rowsDone": index + 1},
            )
        return {"rows": rows}

    plugin = LitestarMCP(MCPConfig(tasks=True))
    app = Litestar(route_handlers=[crunch], plugins=[plugin])
    original = plugin.registry.publish_notification

    async def recording_publish(method: str, params: dict[str, Any]) -> None:
        subscription_publications.append((method, params))
        await original(method, params)

    plugin.registry.publish_notification = recording_publish  # type: ignore[method-assign]
    return app, subscription_publications


def test_request_without_progress_token_keeps_the_plain_json_path_byte_equivalent() -> None:
    app, published = _make_app()
    with TestClient(app=app) as client:
        without_token = _rpc(client, "tools/call", {"name": "crunch", "arguments": {"rows": 2}})
        token_without_sse = _rpc(
            client,
            "tools/call",
            {"name": "crunch", "arguments": {"rows": 2}},
            progress_token="ignored-without-accept",
        )

    assert without_token == token_without_sse
    assert without_token["result"]["resultType"] == "complete"
    assert published == []


def test_progress_token_and_sse_accept_yield_notifications_before_the_final_response() -> None:
    app, published = _make_app()
    with TestClient(app=app) as client:
        response, messages = _sse_rpc(
            client,
            "tools/call",
            {"name": "crunch", "arguments": {"rows": 3}},
            progress_token="tok-1",
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
    assert response.headers["X-Accel-Buffering"] == "no"
    progress = [message["params"] for message in messages[:-1]]
    assert [event["progressToken"] for event in progress] == ["tok-1", "tok-1", "tok-1"]
    assert [event["progress"] for event in progress] == [1, 2, 3]
    assert all(event["total"] == 3 for event in progress)
    assert progress[-1]["message"] == "row 3 of 3"
    assert progress[-1]["_meta"] == {"rowsDone": 3}
    assert messages[-1]["result"]["resultType"] == "complete"
    assert published == []


def test_task_mode_stream_ends_at_create_task_result_and_late_progress_is_dropped() -> None:
    app, published = _make_app()
    with TestClient(app=app) as client:
        _response, messages = _sse_rpc(
            client,
            "tools/call",
            {"name": "crunch", "arguments": {"rows": 2}},
            progress_token=7,
            tasks_capable=True,
        )
        assert len(messages) == 1
        created = messages[0]["result"]
        assert created["resultType"] == "task"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            record = _rpc(client, "tasks/get", {"taskId": created["taskId"]}, tasks_capable=True)["result"]
            if record["status"] == "completed":
                break
            time.sleep(0.01)
        else:
            msg = "task did not complete"
            raise AssertionError(msg)

    assert all(method == "notifications/tasks" for method, _params in published)
    assert not any(method == "notifications/progress" for method, _params in published)


@pytest.mark.anyio
async def test_stream_close_makes_retained_reporters_no_ops() -> None:
    stream = RequestNotificationStream()
    reporter = ProgressReporter("tok", stream.publish)
    await reporter.report(1)
    stream.close()
    await reporter.report(2)

    messages = [message async for message in stream]
    assert [message["params"]["progress"] for message in messages] == [1]
    assert stream.closed is True


@pytest.mark.anyio
async def test_response_generator_teardown_cancels_in_flight_dispatch() -> None:
    stream = RequestNotificationStream()
    cancelled = asyncio.Event()

    async def dispatch() -> dict[str, Any]:
        await stream.publish("notifications/progress", progress_params("tok", 1))
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    generator = _request_event_stream(dispatch, stream)
    first = await generator.__anext__()
    assert isinstance(first, ServerSentEventMessage)
    assert '"method":"notifications/progress"' in cast("str", first.data)
    await generator.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert stream.closed is True


@pytest.mark.anyio
async def test_reporter_without_token_or_publish_is_a_no_op() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def publish(method: str, params: dict[str, Any]) -> None:
        calls.append((method, params))

    await ProgressReporter(None, publish).report(1, total=2)
    await ProgressReporter("tok", None).report(1, total=2)
    assert calls == []

    await ProgressReporter("tok", publish).report(0.5, total=1.0, message="half", meta={"k": "v"})
    assert calls == [
        (
            "notifications/progress",
            {"progressToken": "tok", "progress": 0.5, "total": 1.0, "message": "half", "_meta": {"k": "v"}},
        )
    ]


def test_progress_params_omits_absent_fields() -> None:
    assert progress_params("tok", 1) == {"progressToken": "tok", "progress": 1}
    assert progress_params(3, 1, total=2) == {"progressToken": 3, "progress": 1, "total": 2}
