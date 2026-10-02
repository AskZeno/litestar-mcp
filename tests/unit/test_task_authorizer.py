"""Task authorizer tests: a shared owner scope with per-action permission checks.

Contract:
    Given a host whose owner key names a shared scope rather than one principal
    When another principal in that scope acts on a task it did not create
    Then ``MCPTaskConfig.authorizer`` decides each action: ``allowed`` proceeds,
    ``not_found`` answers as for an unknown task, and ``forbidden`` refuses the
    action with ``error.data.statusCode == 403``; ``subscriptions/listen``
    drops every id the authorizer does not allow

Invariants:
    - The owner check runs first: the authorizer never sees a foreign record.
    - ``MCPTaskConfig.creator_resolver`` records provenance on the stored
      record and never exposes it on the wire.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, cast

import pytest
from litestar import Litestar, Request, get
from litestar.middleware import ASGIMiddleware
from litestar.stores.memory import MemoryStore
from litestar.testing import TestClient
from litestar.types import ASGIApp, Receive, Scope, Send

from litestar_mcp import LitestarMCP, MCPConfig, MCPTaskConfig, MCPTaskStore, TaskAccess, TaskAction, TaskRecord
from litestar_mcp.utils import mcp_tool

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


@dataclass(frozen=True)
class _Member:
    workspace: str
    member: str
    role: str


class _MemberMiddleware(ASGIMiddleware):
    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        headers = dict(scope["headers"])
        workspace, member, role = (headers.get(name) for name in (b"x-workspace", b"x-member", b"x-role"))
        if workspace is not None and member is not None and role is not None:
            scope["user"] = _Member(workspace=workspace.decode(), member=member.decode(), role=role.decode())
        await next_app(scope, receive, send)


def _member(request: "Request[Any, Any, Any]") -> "_Member | None":
    user = request.scope.get("user")
    return user if isinstance(user, _Member) else None


def _workspace(request: "Request[Any, Any, Any]") -> "str | None":
    member = _member(request)
    return None if member is None else f"workspace:{member.workspace}"


def _creator(request: "Request[Any, Any, Any]") -> "str | None":
    member = _member(request)
    return None if member is None else f"member:{member.member}"


seen: list[tuple[str | None, TaskAction]] = []


async def _authorize(request: "Request[Any, Any, Any]", record: TaskRecord, action: TaskAction) -> TaskAccess:
    seen.append((record.owner_id, action))
    member = _member(request)
    if member is None or member.role == "guest":
        return "not_found"
    if action in {"cancel", "update"} and member.role != "editor":
        return "forbidden"
    return "allowed"


class _AcknowledgingSubscriptions:
    async def open(self, subscription_id: Any, notifications: dict[str, Any]) -> Any:
        async def stream() -> Any:
            yield {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {"notifications": notifications},
            }

        return "finite", stream()

    async def publish(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def disconnect(self, stream_id: str) -> None:
        return None


def _make_app(store: MemoryStore | None = None) -> Litestar:
    @get("/slow-task")
    @mcp_tool(name="slow_task", task_support="optional")
    async def slow_task(delay: float = 1.0) -> dict[str, str]:
        await asyncio.sleep(delay)
        return {"status": "completed"}

    plugin = LitestarMCP(
        MCPConfig(
            tasks=MCPTaskConfig(
                store=store,
                owner_resolver=_workspace,
                creator_resolver=_creator,
                authorizer=_authorize,
            )
        )
    )
    app = Litestar(route_handlers=[slow_task], middleware=[_MemberMiddleware()], plugins=[plugin])
    plugin.registry.set_subscription_manager(_AcknowledgingSubscriptions())  # type: ignore[arg-type]
    return app


def _meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
        "io.modelcontextprotocol/clientInfo": {"name": "authorizer-tests", "version": "1"},
    }


def _as(workspace: str, member: str, role: str) -> dict[str, str]:
    return {"x-workspace": workspace, "x-member": member, "x-role": role}


def _rpc(client: TestClient[Any], method: str, params: dict[str, Any], who: dict[str, str]) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Mcp-Name": str(params.get("name", params.get("taskId", ""))),
        **who,
    }
    return cast(
        "dict[str, Any]",
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": _meta()}},
            headers=headers,
        ).json(),
    )


def _listen(client: TestClient[Any], task_ids: list[str], who: dict[str, str]) -> dict[str, Any]:
    with client.stream(
        "POST",
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "subscriptions/listen",
            "params": {"_meta": _meta(), "notifications": {"taskIds": task_ids}},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "subscriptions/listen",
            **who,
        },
    ) as response:
        data_line = next(line for line in response.iter_lines() if line.startswith("data: "))
    payload = cast("dict[str, Any]", json.loads(data_line.partition("data: ")[2]))
    return cast("dict[str, Any]", payload["params"]["notifications"])


def _create_task(client: TestClient[Any], who: dict[str, str]) -> str:
    created = _rpc(client, "tools/call", {"name": "slow_task", "arguments": {"delay": 1.0}}, who)
    assert created["result"]["resultType"] == "task"
    return cast("str", created["result"]["taskId"])


CREATOR = _as("w1", "alice", "editor")


def test_another_editor_in_the_scope_can_get_listen_and_cancel() -> None:
    colleague = _as("w1", "bob", "editor")
    with TestClient(app=_make_app()) as client:
        task_id = _create_task(client, CREATOR)

        fetched = _rpc(client, "tasks/get", {"taskId": task_id}, colleague)
        acknowledged = _listen(client, [task_id], colleague)
        cancelled = _rpc(client, "tasks/cancel", {"taskId": task_id}, colleague)

    assert fetched["result"]["taskId"] == task_id
    assert acknowledged == {"taskIds": [task_id]}
    assert cancelled["result"]["resultType"] == "complete"


def test_a_forbidden_action_is_refused_with_status_403_while_reads_proceed() -> None:
    viewer = _as("w1", "carol", "viewer")
    with TestClient(app=_make_app()) as client:
        task_id = _create_task(client, CREATOR)

        fetched = _rpc(client, "tasks/get", {"taskId": task_id}, viewer)
        cancelled = _rpc(client, "tasks/cancel", {"taskId": task_id}, viewer)
        updated = _rpc(client, "tasks/update", {"taskId": task_id, "inputResponses": {}}, viewer)
        still_working = _rpc(client, "tasks/get", {"taskId": task_id}, CREATOR)

    assert fetched["result"]["taskId"] == task_id
    for refused in (cancelled, updated):
        assert refused["error"]["message"] == "Task access denied"
        assert refused["error"]["data"] == {"statusCode": 403}
    assert still_working["result"]["status"] == "working"


def test_a_hidden_task_answers_as_unknown_and_is_dropped_from_listen() -> None:
    guest = _as("w1", "dave", "guest")
    with TestClient(app=_make_app()) as client:
        task_id = _create_task(client, CREATOR)

        fetched = _rpc(client, "tasks/get", {"taskId": task_id}, guest)
        cancelled = _rpc(client, "tasks/cancel", {"taskId": task_id}, guest)
        acknowledged = _listen(client, [task_id], guest)

    for hidden in (fetched, cancelled):
        assert hidden["error"]["message"] == "Failed to retrieve task: Task not found"
        assert "data" not in hidden["error"]
    assert acknowledged == {"taskIds": []}


def test_the_owner_check_runs_before_the_authorizer() -> None:
    seen.clear()
    outsider = _as("w2", "erin", "editor")
    with TestClient(app=_make_app()) as client:
        task_id = _create_task(client, CREATOR)

        fetched = _rpc(client, "tasks/get", {"taskId": task_id}, outsider)
        cancelled = _rpc(client, "tasks/cancel", {"taskId": task_id}, outsider)
        acknowledged = _listen(client, [task_id], outsider)

    assert fetched["error"]["message"] == "Failed to retrieve task: Task not found"
    assert cancelled["error"]["message"] == "Failed to retrieve task: Task not found"
    assert acknowledged == {"taskIds": []}
    assert seen == []


def test_the_creator_is_stored_for_provenance_but_never_sent() -> None:
    store = MemoryStore()
    with TestClient(app=_make_app(store)) as client:
        task_id = _create_task(client, CREATOR)
        fetched = _rpc(client, "tasks/get", {"taskId": task_id}, _as("w1", "bob", "editor"))

    record = asyncio.run(MCPTaskStore(store=store).load(task_id))
    assert (record.owner_id, record.creator_id) == ("workspace:w1", "member:alice")
    assert "creatorId" not in fetched["result"]
    assert "alice" not in json.dumps(fetched)


@pytest.mark.anyio
async def test_store_round_trips_the_creator() -> None:
    store = MCPTaskStore()
    record = await store.create("workspace:w1", creator_id="member:alice")

    assert (await store.load(record.task_id)).creator_id == "member:alice"
    assert "creatorId" not in record.to_dict()
