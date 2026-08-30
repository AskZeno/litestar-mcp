"""Execution-backend seam tests: records persist in the store, work runs in the backend.

Contract:
    Given a task record carries namespaced metadata
    When its backend updates and terminally settles the task
    Then CreateTaskResult, tasks/get, and notifications observe the same
    metadata-preserving record

Invariants:
    - Metadata updates preserve unrelated namespaces.
    - Terminal status, metadata, and result/error persist in one record write.
    - Terminal records never reopen or accept late metadata.
    - The task store remains product-neutral.
"""

import json
import time
from typing import Any, cast

import pytest
from litestar import Litestar, get
from litestar.testing import TestClient

from litestar_mcp import (
    AsyncioTaskBackend,
    LitestarMCP,
    MCPConfig,
    MCPTaskConfig,
    MCPTaskStore,
    TaskExecutionBackend,
    TaskInvocation,
    TaskRecord,
)
from litestar_mcp.jsonrpc import JSONRPCError
from litestar_mcp.utils import mcp_tool

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


class MetadataBackend(TaskExecutionBackend):
    """Backend that enriches the durable record before its handle is returned."""

    async def start(self, task: TaskRecord, request: TaskInvocation) -> TaskRecord:
        return await self.store.record_status(
            task.task_id,
            "working",
            status_message="2 of 4",
            meta={"example.test/progress": {"completed": 2, "total": 4}},
        )

    async def cancel(self, task_id: str) -> None:
        await self.store.mark_cancelled(task_id)

    async def deliver_input(self, task_id: str, payload: dict[str, Any]) -> None:
        return None


class RecordingBackend(TaskExecutionBackend):
    """Backend double standing in for a durable execution engine."""

    def __init__(self) -> None:
        self.started: list[TaskInvocation] = []
        self.cancelled: list[str] = []
        self.delivered: list[tuple[str, dict[str, Any]]] = []

    async def start(self, task: TaskRecord, request: TaskInvocation) -> None:
        self.started.append(request)

    async def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)
        await self.store.mark_cancelled(task_id)

    async def deliver_input(self, task_id: str, payload: dict[str, Any]) -> None:
        self.delivered.append((task_id, payload))
        await self.store.complete(task_id, {"resultType": "complete", "content": [], "isError": False})


def _rpc(
    client: TestClient[Any],
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_params = dict(params or {})
    request_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
        "io.modelcontextprotocol/clientInfo": {"name": "backend-tests", "version": "1"},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    name_field = {
        "tools/call": "name",
        "tasks/get": "taskId",
        "tasks/update": "taskId",
        "tasks/cancel": "taskId",
    }.get(method)
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


def _make_app(backend: TaskExecutionBackend) -> Litestar:
    @get("/work", sync_to_thread=False)
    @mcp_tool(name="work", task_support="optional")
    async def work() -> dict[str, str]:
        return {"status": "done"}

    return Litestar(
        route_handlers=[work],
        plugins=[LitestarMCP(MCPConfig(tasks=MCPTaskConfig(execution_backend=backend)))],
    )


def _wait_for_status(client: TestClient[Any], task_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = _rpc(client, "tasks/get", {"taskId": task_id})["result"]
        if result["status"] == status:
            return cast("dict[str, Any]", result)
        time.sleep(0.01)
    msg = f"task {task_id} did not reach {status}"
    raise AssertionError(msg)


def test_created_handle_and_tasks_get_share_backend_metadata() -> None:
    backend = MetadataBackend()
    with TestClient(app=_make_app(backend)) as client:
        created = _rpc(client, "tools/call", {"name": "work", "arguments": {}})["result"]
        observed = _rpc(client, "tasks/get", {"taskId": created["taskId"]})["result"]

    expected = {"completed": 2, "total": 4}
    assert created["_meta"]["example.test/progress"] == expected
    assert observed["_meta"] == created["_meta"]
    assert "io.modelcontextprotocol/serverInfo" in created["_meta"]


@pytest.mark.anyio
async def test_metadata_merges_and_terminal_settlement_rejects_late_updates() -> None:
    notifications: list[dict[str, Any]] = []

    async def capture(record: TaskRecord) -> None:
        notifications.append(record.to_dict())

    store = MCPTaskStore(status_callback=capture)
    record = await store.create(owner_id=None)
    await store.record_status(record.task_id, "working", meta={"example.test/progress": {"completed": 1}})
    settled = await store.complete(
        record.task_id,
        {"resultType": "complete", "content": [], "isError": False},
        meta={"example.test/resources": [{"uri": "test://resource/1", "edge": "mutated"}]},
    )
    late = await store.record_status(
        record.task_id,
        "failed",
        error=JSONRPCError(code=-32603, message="late"),
        meta={"example.test/progress": {"completed": 0}},
    )
    recovered = await store.get(record.task_id, None)

    expected_meta = {
        "example.test/progress": {"completed": 1},
        "example.test/resources": [{"uri": "test://resource/1", "edge": "mutated"}],
    }
    assert settled.meta == expected_meta
    assert recovered.meta == expected_meta
    assert late.status == "completed"
    assert late.error is None
    assert notifications[-1]["_meta"] == expected_meta


def test_tools_call_launches_created_tasks_through_the_configured_backend() -> None:
    backend = RecordingBackend()
    with TestClient(app=_make_app(backend)) as client:
        created = _rpc(client, "tools/call", {"name": "work", "arguments": {}})["result"]

    assert created["resultType"] == "task"
    assert len(backend.started) == 1
    invocation = backend.started[0]
    assert invocation.task_id == created["taskId"]
    assert invocation.tool_name == "work"
    assert invocation.arguments == {}


def test_tasks_cancel_delegates_to_the_backend_and_converges_when_repeated() -> None:
    backend = RecordingBackend()
    with TestClient(app=_make_app(backend)) as client:
        created = _rpc(client, "tools/call", {"name": "work", "arguments": {}})["result"]
        first = _rpc(client, "tasks/cancel", {"taskId": created["taskId"]})
        terminal = _wait_for_status(client, created["taskId"], "cancelled")
        second = _rpc(client, "tasks/cancel", {"taskId": created["taskId"]})
        after = _rpc(client, "tasks/get", {"taskId": created["taskId"]})["result"]

    assert first["result"]["resultType"] == "complete"
    assert second["result"]["resultType"] == "complete"
    assert terminal["status"] == "cancelled"
    assert after["status"] == "cancelled"
    assert backend.cancelled == [created["taskId"]]


def test_tasks_update_forwards_only_fully_merged_responses_to_the_backend() -> None:
    backend = RecordingBackend()
    app = _make_app(backend)
    plugin = next(p for p in app.plugins.init if isinstance(p, LitestarMCP))
    store = plugin.task_store
    assert store is not None
    with TestClient(app=app) as client:
        created = _rpc(client, "tools/call", {"name": "work", "arguments": {}})["result"]
        task_id = created["taskId"]

        async def require_two_inputs() -> None:
            await store.require_input(
                task_id,
                {
                    "first": {"method": "elicitation/create", "params": {}},
                    "second": {"method": "elicitation/create", "params": {}},
                },
                None,
            )

        with client.portal() as portal:
            portal.call(require_two_inputs)
        partial = _rpc(client, "tasks/update", {"taskId": task_id, "inputResponses": {"first": "a"}})
        assert partial["result"]["resultType"] == "complete"
        assert backend.delivered == []
        _rpc(client, "tasks/update", {"taskId": task_id, "inputResponses": {"second": "b"}})
        _wait_for_status(client, task_id, "completed")

    assert backend.delivered == [(task_id, {"first": "a", "second": "b"})]


@pytest.mark.anyio
async def test_record_status_is_the_single_writer_and_terminal_records_never_reopen() -> None:
    notified: list[str] = []
    store = MCPTaskStore()

    async def on_status(record: TaskRecord) -> None:
        notified.append(record.status)

    store.set_status_callback(on_status)
    record = await store.create(owner_id=None)
    await store.record_status(record.task_id, "completed", result={"resultType": "complete"})
    reopened = await store.record_status(
        record.task_id,
        "failed",
        error=JSONRPCError(code=-32603, message="late failure"),
    )
    final = await store.get(record.task_id, None)

    assert notified == ["working", "completed"]
    assert reopened.status == "completed"
    assert final.status == "completed"
    assert final.error is None


def test_default_backend_is_the_asyncio_runner_and_preserves_prior_behavior() -> None:
    app = Litestar(plugins=[LitestarMCP(MCPConfig(tasks=True))])
    plugin = next(p for p in app.plugins.init if isinstance(p, LitestarMCP))
    assert isinstance(plugin.task_backend, AsyncioTaskBackend)
    assert plugin.task_backend.store is plugin.task_store


def _listen(client: TestClient[Any], notifications: dict[str, Any], *, tasks_capable: bool) -> Any:
    capabilities: dict[str, Any] = {}
    if tasks_capable:
        capabilities["extensions"] = {TASKS_EXTENSION: {}}
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "subscriptions/listen",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": capabilities,
                    "io.modelcontextprotocol/clientInfo": {"name": "backend-tests", "version": "1"},
                },
                "notifications": notifications,
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "subscriptions/listen",
        },
    )


def test_task_id_subscriptions_require_the_tasks_capability() -> None:
    backend = RecordingBackend()
    with TestClient(app=_make_app(backend)) as client:
        rejected = _listen(client, {"taskIds": ["task-1"], "toolsListChanged": True}, tasks_capable=False)

    assert rejected.status_code == 400
    error = rejected.json()["error"]
    assert error["code"] == -32021
    assert error["data"]["requiredCapabilities"]["extensions"] == {TASKS_EXTENSION: {}}


def test_declaring_listeners_keep_their_task_id_filter() -> None:
    class FiniteSubscriptions:
        async def open(self, subscription_id: Any, notifications: dict[str, Any]) -> Any:
            async def stream() -> Any:
                yield {
                    "jsonrpc": "2.0",
                    "method": "notifications/subscriptions/acknowledged",
                    "params": {"notifications": notifications},
                }

            return "finite", stream()

        async def disconnect(self, stream_id: str) -> None:
            return None

    backend = RecordingBackend()
    app = _make_app(backend)
    plugin = next(p for p in app.plugins.init if isinstance(p, LitestarMCP))
    plugin.registry.set_subscription_manager(FiniteSubscriptions())  # type: ignore[arg-type]
    with (
        TestClient(app=app) as client,
        client.stream(
            "POST",
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "subscriptions/listen",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
                        "io.modelcontextprotocol/clientInfo": {"name": "backend-tests", "version": "1"},
                    },
                    "notifications": {"taskIds": ["task-1"]},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "subscriptions/listen",
            },
        ) as response,
    ):
        data_line = next(line for line in response.iter_lines() if line.startswith("data: "))
        payload = json.loads(data_line.partition("data: ")[2])

    assert payload["method"] == "notifications/subscriptions/acknowledged"
    assert payload["params"]["notifications"] == {"taskIds": ["task-1"]}
