"""Request-scoped ``notifications/progress`` reporting.

A request carrying ``params._meta.progressToken`` can receive progress on
its own response channel. :class:`ProgressReporter` owns the protocol
envelope while :class:`RequestNotificationStream` owns one request's
bounded lifetime. The transport supplies its ``publish`` callable; after
the final response (or disconnect) the stream closes and later reports
become safe no-ops.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = (
    "ProgressPublish",
    "ProgressReporter",
    "RequestNotificationStream",
    "progress_params",
)

ProgressPublish = Callable[[str, "dict[str, Any]"], "Awaitable[None]"]
"""Publish one notification as ``(method, params)``."""

_CLOSED = object()


class RequestNotificationStream:
    """One request's notification queue with an explicit terminal boundary.

    The queue is intentionally unbounded: progress publishing must not
    deadlock a tool when the transport is briefly unable to flush. A
    closed stream drops later publications, which is particularly
    important for task backends that retain the creating request's
    reporter after ``CreateTaskResult`` has ended that request.
    """

    def __init__(self) -> "None":
        self._queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
        self._closed = False

    @property
    def closed(self) -> "bool":
        """Whether the request response has reached its terminal boundary."""
        return self._closed

    async def publish(self, method: "str", params: "dict[str, Any]") -> "None":
        """Queue one JSON-RPC notification, or no-op after close."""
        if self._closed:
            return
        self._queue.put_nowait({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> "None":
        """End iteration exactly once; publications after this point are dropped."""
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(_CLOSED)

    def __aiter__(self) -> "AsyncIterator[dict[str, Any]]":
        return self._iterate()

    async def _iterate(self) -> "AsyncIterator[dict[str, Any]]":
        while True:
            message = await self._queue.get()
            if message is _CLOSED:
                return
            yield message  # type: ignore[misc]


def progress_params(
    progress_token: "str | int",
    progress: "float",
    total: "float | None" = None,
    message: "str | None" = None,
    *,
    meta: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """Build the standard ``notifications/progress`` params envelope."""
    params: dict[str, Any] = {"progressToken": progress_token, "progress": progress}
    if total is not None:
        params["total"] = total
    if message is not None:
        params["message"] = message
    if meta is not None:
        params["_meta"] = meta
    return params


@dataclass
class ProgressReporter:
    """Emit ``notifications/progress`` for one request's progress token.

    ``report`` is safe to call unconditionally: without a token or a
    request-owned publish target it does nothing, so tools never branch
    on whether the client requested and negotiated a delivery lane.
    """

    progress_token: "str | int | None" = None
    publish: "ProgressPublish | None" = None

    async def report(
        self,
        progress: "float",
        total: "float | None" = None,
        message: "str | None" = None,
        *,
        meta: "dict[str, Any] | None" = None,
    ) -> "None":
        """Emit one progress notification when the request opted in."""
        if self.progress_token is None or self.publish is None:
            return
        await self.publish(
            "notifications/progress",
            progress_params(self.progress_token, progress, total, message, meta=meta),
        )
