"""Progress notification tests: token threading, envelope shape, no-token silence."""

import time
from typing import Any, cast

import pytest
from litestar import Litestar, get
from litestar.testing import TestClient

from litestar_mcp import (
    LitestarMCP,
    MCPConfig,
    ProgressReporter,
    get_mcp_request_context,
    progress_params,
)
from litestar_mcp.utils import mcp_tool

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


def _rpc(
    client: TestClient[Any],
    method: str,
    params: dict[str, Any] | None = None,
    *,
    progress_token: str | int | None = None,
    tasks_capable: bool = False,
) -> dict[str, Any]:
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
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    name_field = {"tools/call": "name", "tasks/get": "taskId"}.get(method)
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


def _make_app() -> tuple[Litestar, list[tuple[str, dict[str, Any]]]]:
    published: list[tuple[str, dict[str, Any]]] = []

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
        published.append((method, params))
        await original(method, params)

    plugin.registry.publish_notification = recording_publish  # type: ignore[method-assign]
    return app, published


def _progress_events(published: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [params for method, params in published if method == "notifications/progress"]


def test_request_without_progress_token_emits_no_progress_notifications() -> None:
    app, published = _make_app()
    with TestClient(app=app) as client:
        result = _rpc(client, "tools/call", {"name": "crunch", "arguments": {"rows": 2}})["result"]

    assert result["resultType"] == "complete"
    assert _progress_events(published) == []


def test_progress_token_yields_monotonic_envelopes_with_meta_detail() -> None:
    app, published = _make_app()
    with TestClient(app=app) as client:
        result = _rpc(
            client,
            "tools/call",
            {"name": "crunch", "arguments": {"rows": 3}},
            progress_token="tok-1",
        )["result"]

    events = _progress_events(published)
    assert result["resultType"] == "complete"
    assert [event["progressToken"] for event in events] == ["tok-1", "tok-1", "tok-1"]
    values = [event["progress"] for event in events]
    assert values == sorted(values)
    assert all(event["total"] == 3 for event in events)
    assert events[-1]["message"] == "row 3 of 3"
    assert events[-1]["_meta"] == {"rowsDone": 3}


def test_task_execution_reports_progress_through_the_invocation_token() -> None:
    app, published = _make_app()
    with TestClient(app=app) as client:
        created = _rpc(
            client,
            "tools/call",
            {"name": "crunch", "arguments": {"rows": 2}},
            progress_token=7,
            tasks_capable=True,
        )["result"]
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

    events = _progress_events(published)
    assert [event["progressToken"] for event in events] == [7, 7]
    assert [event["progress"] for event in events] == [1, 2]


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
