"""Pluggable execution backends for the ``io.modelcontextprotocol/tasks`` extension.

:class:`~litestar_mcp.tasks.MCPTaskStore` persists task *records*; a
:class:`TaskExecutionBackend` owns how a created task's *work* is
launched, cancelled, and fed input. The default backend runs work on
in-process asyncio tasks (the pre-seam behavior). Deployments whose
long-running work already lives in a durable execution engine implement
the protocol over that machinery and report every transition through the
bound store's :meth:`~litestar_mcp.tasks.MCPTaskStore.record_status`
writer, which persists the record and fans out task-status notifications.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from litestar_mcp.jsonrpc import INTERNAL_ERROR, JSONRPCError, JSONRPCErrorException

if TYPE_CHECKING:
    from litestar_mcp.tasks import MCPTaskStore, TaskRecord

__all__ = (
    "AsyncioTaskBackend",
    "TaskExecutionBackend",
    "TaskInvocation",
    "ToolPass",
)

ToolPass = Callable[["dict[str, Any] | None", "str | None"], "Awaitable[dict[str, Any]]"]
"""One in-process pass of a tool: ``(input_responses, request_state) -> result``."""


@dataclass
class TaskInvocation:
    """One created task's launch request.

    ``run_tool`` executes a single in-process pass of the tool and returns
    the MCP tool result mapping. Backends that execute the work elsewhere
    (workflow engines, queues) key off ``tool_name``/``arguments`` and may
    ignore it.
    """

    task_id: "str"
    tool_name: "str"
    arguments: "dict[str, Any]"
    owner_id: "str | None"
    run_tool: "ToolPass"


class TaskExecutionBackend(ABC):
    """Owns launching, cancelling, and feeding input to task work.

    A backend never persists task state itself: every status transition
    flows through the bound store's ``record_status`` writer.
    """

    _store: "MCPTaskStore | None" = None

    def bind(self, store: "MCPTaskStore") -> "None":
        """Bind the record store whose ``record_status`` receives transitions."""
        self._store = store

    @property
    def store(self) -> "MCPTaskStore":
        """The bound record store."""
        if self._store is None:
            msg = "Task execution backend is not bound to a task store"
            raise RuntimeError(msg)
        return self._store

    @abstractmethod
    async def start(self, task: "TaskRecord", request: "TaskInvocation") -> "None":
        """Launch the work for a created task record."""

    @abstractmethod
    async def cancel(self, task_id: "str") -> "None":
        """Request cancellation through the backend's authority."""

    @abstractmethod
    async def deliver_input(self, task_id: "str", payload: "dict[str, Any]") -> "None":
        """Forward a completed set of ``input_required`` responses."""

    async def close(self) -> "None":  # noqa: B027 - optional hook, default no-op
        """Release process-local execution resources on shutdown."""


class AsyncioTaskBackend(TaskExecutionBackend):
    """The in-process default: one asyncio runner per task.

    Runner tasks, cancellation flags, and input queues are process-local
    coordination primitives; a restart orphans running work. Durable
    deployments should implement :class:`TaskExecutionBackend` over their
    own execution engine instead.
    """

    def __init__(self) -> "None":
        self._runners: dict[str, asyncio.Task[Any]] = {}
        self._input_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def start(self, task: "TaskRecord", request: "TaskInvocation") -> "None":
        self._input_queues.setdefault(request.task_id, asyncio.Queue())
        self._cancel_events.setdefault(request.task_id, asyncio.Event())
        self._runners[request.task_id] = asyncio.create_task(self._drive(request))

    async def cancel(self, task_id: "str") -> "None":
        self._cancel_events.setdefault(task_id, asyncio.Event()).set()
        runner = self._runners.get(task_id)
        if runner is not None:
            runner.cancel()

    async def deliver_input(self, task_id: "str", payload: "dict[str, Any]") -> "None":
        await self._input_queues.setdefault(task_id, asyncio.Queue()).put(payload)

    async def close(self) -> "None":
        runners = list(self._runners.values())
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)

    async def _wait_for_input(self, task_id: "str") -> "dict[str, Any]":
        queue = self._input_queues.setdefault(task_id, asyncio.Queue())
        cancel_event = self._cancel_events.setdefault(task_id, asyncio.Event())
        input_wait = asyncio.create_task(queue.get())
        cancel_wait = asyncio.create_task(cancel_event.wait())
        done, pending = await asyncio.wait({input_wait, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
        for waiter in pending:
            waiter.cancel()
        if cancel_wait in done:
            raise asyncio.CancelledError
        return input_wait.result()

    async def _drive(self, request: "TaskInvocation") -> "None":
        store = self.store
        task_id = request.task_id
        input_responses: dict[str, Any] | None = None
        request_state: str | None = None
        try:
            while True:
                result = await request.run_tool(input_responses, request_state)
                if result.get("resultType") != "input_required":
                    result.setdefault("resultType", "complete")
                    await store.complete(task_id, result)
                    return
                await store.require_input(
                    task_id,
                    result.get("inputRequests"),
                    result.get("requestState"),
                )
                input_responses = await self._wait_for_input(task_id)
                request_state = result.get("requestState")
        except JSONRPCErrorException as exc:
            await store.fail(task_id, exc.error)
        except asyncio.CancelledError:
            await store.mark_cancelled(task_id)
        except Exception as exc:  # noqa: BLE001
            await store.fail(
                task_id,
                JSONRPCError(code=INTERNAL_ERROR, message=str(exc)),
                status_message=str(exc),
            )
        finally:
            self._runners.pop(task_id, None)
            self._input_queues.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
