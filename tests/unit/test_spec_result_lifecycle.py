"""Specification results keep their wire shape without skipping send hooks."""

from typing import TYPE_CHECKING, Any

import pytest
from litestar import Litestar, Request, post
from litestar.response import Response
from litestar.testing import TestClient
from pydantic import BaseModel, Field

from litestar_mcp import LitestarMCP, MCPConfig
from litestar_mcp.executor import execute_tool

if TYPE_CHECKING:
    from litestar.types import Message, Scope


class SpecResult(BaseModel):
    """Structural CallToolResult model, without a dependency on a spec package."""

    content: list[dict[str, Any]] = Field(default_factory=list)
    structured_content: dict[str, Any] | None = Field(default=None, alias="structuredContent")
    is_error: bool = Field(default=False, alias="isError")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize("observer_fails", [False, True])
def test_spec_result_runs_send_hooks_once_before_observers(is_error: bool, observer_fails: bool) -> None:
    events: list[str] = []
    statuses: list[int] = []
    spec_result = SpecResult(
        content=[
            {"type": "text", "text": "unchanged"},
            {"type": "resource_link", "uri": "app://items/7", "name": "item"},
        ],
        structuredContent={"id": "7"},
        isError=is_error,
        _meta={"example/trace": "trace-id"},
    )

    async def before_request(request: Request[Any, Any, Any]) -> None:
        request.scope["state"]["opened"] = True
        events.append("before_request")

    async def before_send(message: "Message", scope: "Scope") -> None:
        if not scope.get("litestar_mcp.internal_dispatch"):
            return
        assert scope["path"] == "/build"
        assert scope["state"]["opened"] is True
        events.append(message["type"])
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
        elif message["type"] == "http.response.body":
            assert message["body"] == b""
            assert not message.get("more_body", False)
            scope["state"]["closed"] = True

    def after_request(response: Response[Any]) -> Response[Any]:
        events.append("after_request")
        return response

    async def after_response(request: Request[Any, Any, Any]) -> None:
        assert request.scope["state"]["closed"] is True
        events.append("after_response")

    def before_tool_call(_name: str, _arguments: dict[str, Any], _request: Request[Any, Any, Any]) -> None:
        events.append("before_tool_call")
        if observer_fails:
            message = "before observer failed"
            raise RuntimeError(message)

    async def after_tool_call(
        name: str,
        arguments: dict[str, Any],
        request: Request[Any, Any, Any],
        *,
        result: Any,
        exception: Exception | None,
        duration: float,
    ) -> None:
        assert name == "build"
        assert arguments == {}
        assert request.scope["state"]["closed"] is True
        assert result is spec_result
        # A returned isError result is still a returned value, not a raised exception.
        assert exception is None
        assert duration >= 0
        events.append("after_tool_call")
        if observer_fails:
            message = "after observer failed"
            raise RuntimeError(message)

    @post(
        "/build",
        mcp_tool="build",
        before_request=before_request,
        after_request=after_request,
        after_response=after_response,
    )
    async def build() -> Any:
        events.append("handler")
        return spec_result

    app = Litestar(
        route_handlers=[build],
        plugins=[LitestarMCP(MCPConfig(before_tool_call=before_tool_call, after_tool_call=after_tool_call))],
        before_send=[before_send],
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "build", "arguments": {}}},
        )

    wire_result = response.json()["result"]
    assert wire_result.pop("resultType") == "complete"
    assert wire_result["_meta"].pop("io.modelcontextprotocol/serverInfo") == {
        "name": "Litestar API",
        "version": "1.0.0",
    }
    assert wire_result == spec_result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert statuses == [500 if is_error else 200]
    assert events == [
        "before_tool_call",
        "before_request",
        "handler",
        "http.response.start",
        "http.response.body",
        "after_response",
        "after_tool_call",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["normal", "spec-success", "spec-error"])
@pytest.mark.parametrize("message_type", ["http.response.start", "http.response.body"])
@pytest.mark.parametrize("observer_fails", [False, True])
async def test_send_failure_preserves_original_exception_and_observer_semantics(
    kind: str, message_type: str, observer_fails: bool
) -> None:
    events: list[str] = []
    observed: list[Exception | None] = []
    original = RuntimeError("required hook failed")
    cleanup_failure = ValueError("fallback cleanup also failed")
    sends = 0

    async def before_send(message: "Message", scope: "Scope") -> None:
        nonlocal sends
        assert scope.get("litestar_mcp.internal_dispatch")
        if message["type"] == message_type:
            sends += 1
            # The existing failure path attempts terminal error cleanup. Even
            # if that cleanup fails too, the first failure must survive intact.
            raise original if sends == 1 else cleanup_failure

    def after_exception(exc: Exception, _scope: "Scope") -> None:
        observed.append(exc)
        events.append("after_exception")
        if observer_fails:
            message = "exception observer failed"
            raise ValueError(message)

    async def after_response(_request: Request[Any, Any, Any]) -> None:
        events.append("after_response")

    def after_tool_call(
        _name: str,
        _arguments: dict[str, Any],
        _request: Request[Any, Any, Any],
        *,
        result: Any,
        exception: Exception | None,
        duration: float,
    ) -> None:
        assert result is None
        assert duration >= 0
        observed.append(exception)
        events.append("after_tool_call")
        if observer_fails:
            message = "after observer failed"
            raise ValueError(message)

    @post("/build", after_response=after_response)
    async def build() -> Any:
        return {"ok": True} if kind == "normal" else SpecResult(isError=kind == "spec-error")

    app = Litestar(route_handlers=[build], before_send=[before_send], after_exception=[after_exception])
    with pytest.raises(RuntimeError) as caught:
        await execute_tool(build, app, {}, config=MCPConfig(after_tool_call=after_tool_call), tool_name="build")

    assert caught.value is original
    assert observed == [original, original]
    assert events == ["after_exception", "after_response", "after_tool_call"]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_spec_send_failure_uses_existing_exception_handler_recovery(is_error: bool) -> None:
    failure = RuntimeError("required hook failed")
    observed: list[Exception] = []
    completions: list[tuple[Any, Exception | None]] = []
    start_count = 0

    async def before_send(message: "Message", _scope: "Scope") -> None:
        nonlocal start_count
        if message["type"] == "http.response.start":
            start_count += 1
            if start_count == 1:
                raise failure

    def after_exception(exc: Exception, _scope: "Scope") -> None:
        observed.append(exc)

    def recover(_request: Request[Any, Any, Any], exc: Exception) -> Response[dict[str, bool]]:
        assert exc is failure
        return Response({"recovered": True}, status_code=200)

    def after_tool_call(
        _name: str,
        _arguments: dict[str, Any],
        _request: Request[Any, Any, Any],
        *,
        result: Any,
        exception: Exception | None,
        duration: float,
    ) -> None:
        completions.append((result, exception))

    @post("/build", exception_handlers={RuntimeError: recover})
    async def build() -> Any:
        return SpecResult(isError=is_error)

    app = Litestar(route_handlers=[build], before_send=[before_send], after_exception=[after_exception])
    result = await execute_tool(build, app, {}, config=MCPConfig(after_tool_call=after_tool_call), tool_name="build")

    assert result == {"recovered": True}
    assert observed == [failure]
    assert completions == [({"recovered": True}, None)]
