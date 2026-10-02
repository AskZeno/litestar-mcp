"""Task ownership tests: a task is visible only to the principal that created it.

Contract:
    Given a task created by one verified principal
    When another principal, or an anonymous request, names its task id
    Then ``tasks/get``, ``tasks/update`` and ``tasks/cancel`` answer "Task not
    found" and ``subscriptions/listen`` drops the id from its filter, while
    the owning principal keeps every capability

Invariants:
    - Hosts whose identity is neither ``auth["sub"]`` nor ``user.id`` /
      ``user.sub`` derive the owner through ``MCPTaskConfig.owner_resolver``.
    - An owned record never matches a request whose owner resolves to ``None``.
    - Trusted server-side readers use ``MCPTaskStore.load``.
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

from litestar_mcp import LitestarMCP, MCPConfig, MCPTaskConfig, MCPTaskStore
from litestar_mcp.tasks import TaskLookupError
from litestar_mcp.utils import mcp_tool

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


@dataclass(frozen=True)
class _Principal:
    """An identity with neither ``id`` nor ``sub``, as host auth may publish."""

    tenant_id: str
    user_id: str


class _PrincipalMiddleware(ASGIMiddleware):
    """Stand-in for host auth: ``x-tenant``/``x-user`` become ``scope["user"]``."""

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        headers = dict(scope["headers"])
        tenant = headers.get(b"x-tenant")
        user = headers.get(b"x-user")
        if tenant is not None and user is not None:
            scope["user"] = _Principal(tenant_id=tenant.decode(), user_id=user.decode())
        await next_app(scope, receive, send)


def _owner(request: "Request[Any, Any, Any]") -> "str | None":
    user = request.scope.get("user")
    if not isinstance(user, _Principal):
        return None
    return f"{user.tenant_id}:{user.user_id}"


class _AcknowledgingSubscriptions:
    """Finite subscription double: acknowledges the filter it was opened with."""

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


def _make_app(task_config: MCPTaskConfig | None = None) -> Litestar:
    @get("/slow-task")
    @mcp_tool(name="slow_task", task_support="optional")
    async def slow_task(delay: float = 1.0) -> dict[str, str]:
        await asyncio.sleep(delay)
        return {"status": "completed"}

    plugin = LitestarMCP(MCPConfig(tasks=task_config or MCPTaskConfig(owner_resolver=_owner)))
    app = Litestar(route_handlers=[slow_task], middleware=[_PrincipalMiddleware()], plugins=[plugin])
    plugin.registry.set_subscription_manager(_AcknowledgingSubscriptions())  # type: ignore[arg-type]
    return app


def _meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
        "io.modelcontextprotocol/clientInfo": {"name": "ownership-tests", "version": "1"},
    }


def _principal(tenant: str | None) -> dict[str, str]:
    return {} if tenant is None else {"x-tenant": tenant, "x-user": "user-1"}


def _rpc(client: TestClient[Any], method: str, params: dict[str, Any], *, tenant: str | None) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Mcp-Name": str(params.get("name", params.get("taskId", ""))),
        **_principal(tenant),
    }
    return cast(
        "dict[str, Any]",
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": _meta()}},
            headers=headers,
        ).json(),
    )


def _listen_acknowledgement(client: TestClient[Any], task_ids: list[str], *, tenant: str | None) -> dict[str, Any]:
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
            **_principal(tenant),
        },
    ) as response:
        data_line = next(line for line in response.iter_lines() if line.startswith("data: "))
    payload = cast("dict[str, Any]", json.loads(data_line.partition("data: ")[2]))
    assert payload["method"] == "notifications/subscriptions/acknowledged"
    return cast("dict[str, Any]", payload["params"]["notifications"])


def _create_task(client: TestClient[Any], *, tenant: str) -> str:
    created = _rpc(client, "tools/call", {"name": "slow_task", "arguments": {"delay": 1.0}}, tenant=tenant)
    assert created["result"]["resultType"] == "task"
    return cast("str", created["result"]["taskId"])


@pytest.mark.parametrize("intruder", ["tenant-b", None], ids=["other-principal", "anonymous"])
def test_task_methods_answer_not_found_to_a_non_owner(intruder: str | None) -> None:
    with TestClient(app=_make_app()) as client:
        task_id = _create_task(client, tenant="tenant-a")

        fetched = _rpc(client, "tasks/get", {"taskId": task_id}, tenant=intruder)
        updated = _rpc(client, "tasks/update", {"taskId": task_id, "inputResponses": {}}, tenant=intruder)
        cancelled = _rpc(client, "tasks/cancel", {"taskId": task_id}, tenant=intruder)
        still_owned = _rpc(client, "tasks/get", {"taskId": task_id}, tenant="tenant-a")

    for response in (fetched, updated, cancelled):
        assert response["error"]["message"] == "Failed to retrieve task: Task not found"
    assert still_owned["result"]["status"] == "working"


def test_owner_keeps_every_task_capability() -> None:
    with TestClient(app=_make_app()) as client:
        task_id = _create_task(client, tenant="tenant-a")

        fetched = _rpc(client, "tasks/get", {"taskId": task_id}, tenant="tenant-a")
        acknowledged = _listen_acknowledgement(client, [task_id], tenant="tenant-a")
        cancelled = _rpc(client, "tasks/cancel", {"taskId": task_id}, tenant="tenant-a")

    assert fetched["result"]["taskId"] == task_id
    assert acknowledged == {"taskIds": [task_id]}
    assert cancelled["result"]["resultType"] == "complete"


@pytest.mark.parametrize("intruder", ["tenant-b", None], ids=["other-principal", "anonymous"])
def test_listen_drops_task_ids_the_caller_does_not_own(intruder: str | None) -> None:
    with TestClient(app=_make_app()) as client:
        task_id = _create_task(client, tenant="tenant-a")
        acknowledged = _listen_acknowledgement(client, [task_id, "never-created"], tenant=intruder)

    assert acknowledged == {"taskIds": []}


def test_owner_resolver_keys_the_stored_record() -> None:
    store = MemoryStore()
    with TestClient(app=_make_app(MCPTaskConfig(store=store, owner_resolver=_owner))) as client:
        task_id = _create_task(client, tenant="tenant-a")

    record = asyncio.run(MCPTaskStore(store=store).load(task_id))
    assert record.owner_id == "tenant-a:user-1"


@pytest.mark.anyio
async def test_store_get_refuses_an_owned_record_without_its_owner() -> None:
    store = MCPTaskStore()
    record = await store.create("tenant-a:user-1")

    with pytest.raises(TaskLookupError, match="Task not found"):
        await store.get(record.task_id, None)
    with pytest.raises(TaskLookupError, match="Task not found"):
        await store.get(record.task_id, "tenant-b:user-1")
    assert (await store.get(record.task_id, "tenant-a:user-1")).task_id == record.task_id
    assert (await store.load(record.task_id)).owner_id == "tenant-a:user-1"
