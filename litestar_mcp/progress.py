"""Mid-execution progress reporting for the ``notifications/progress`` envelope.

A request that carries ``params._meta.progressToken`` opts into progress:
the library threads the token into tool execution context and exposes a
:class:`ProgressReporter` that emits ``notifications/progress`` on the
server's notification stream — from ordinary tools and from task
execution backends alike. Structured, kind-owned detail rides ``_meta``
on the notification; the library standardizes the envelope, never the
detail vocabulary. Without a token the reporter is a no-op.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = (
    "ProgressPublish",
    "ProgressReporter",
    "progress_params",
)

ProgressPublish = Callable[[str, "dict[str, Any]"], "Awaitable[None]"]
"""Publishes one notification: ``(method, params)``."""


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
    """Emits ``notifications/progress`` for one request's progress token.

    ``report`` is safe to call unconditionally: without a token or a
    publish target it does nothing, so tools never branch on whether the
    client asked for progress.
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
