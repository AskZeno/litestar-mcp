"""Additive block-builder failures are observable without payload logging."""

import json
import logging
from typing import Any

import pytest
from litestar import Litestar, Request, post
from litestar.response import Response

from litestar_mcp import MCPConfig
from litestar_mcp.config import MCPOptKeys
from litestar_mcp.services.handler import MCPHandlerService, RequestContext


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile_identifiers", [False, True])
async def test_builder_failure_warns_without_changing_result_or_observer_payload(
    caplog: pytest.LogCaptureFixture, hostile_identifiers: bool
) -> None:
    tool_name = "create_table" if not hostile_identifiers else "create\n\x1b[31m" + "x" * 300
    opt_key = "mcp_result_blocks" if not hostile_identifiers else "blocks\t\x00" + "y" * 300
    argument_secret = "private-argument"
    result_secret = "private-result"
    exception_secret = "private-exception"
    calls: list[Any] = []
    observed: list[tuple[Any, Exception | None]] = []

    def explode(value: Any) -> list[dict[str, Any]]:
        calls.append(value)
        message = f"{exception_secret}: {argument_secret}: {value!r}"
        raise RuntimeError(message)

    def after_tool_call(
        _name: str,
        _arguments: dict[str, Any],
        _request: Request[Any, Any, Any],
        *,
        result: Any,
        exception: Exception | None,
        duration: float,
    ) -> None:
        observed.append((result, exception))

    @post("/tables", opt={opt_key: explode})
    async def create_table(argument: str) -> dict[str, str]:
        assert argument == argument_secret
        return {"table_id": result_secret}

    app = Litestar(route_handlers=[create_table], logging_config=None)
    service = MCPHandlerService(
        config=MCPConfig(opt_keys=MCPOptKeys(tool_result_blocks=opt_key), after_tool_call=after_tool_call),
        discovered_tools={tool_name: create_table},
        discovered_resources={},
        discovered_prompts={},
        app_ref=app,
        registry=None,
    )
    with caplog.at_level(logging.WARNING, logger="litestar_mcp.services.handler"):
        result = await service.tools_call(
            {"name": tool_name, "arguments": {"argument": argument_secret}},
            RequestContext(client_id="test", owner_id=None),
        )

    assert result == {
        "content": [{"type": "text", "text": json.dumps({"table_id": result_secret}, separators=(",", ":"))}],
        "structuredContent": {"table_id": result_secret},
        "isError": False,
    }
    assert calls == [{"table_id": result_secret}]
    assert observed == [({"table_id": result_secret}, None)]
    records = [record for record in caplog.records if record.name == "litestar_mcp.services.handler"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert isinstance(record.args, tuple)
    assert len(record.args) == 2
    for field in record.args:
        assert isinstance(field, str)
        assert len(field) <= 128
        assert all(character.isascii() and (character.isalnum() or character in "_.:-") for character in field)
    if not hostile_identifiers:
        assert record.args == (tool_name, opt_key)
    assert all(secret not in str(record.__dict__) for secret in (argument_secret, result_secret, exception_secret))
    assert all(secret not in caplog.text for secret in (argument_secret, result_secret, exception_secret))


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_error_paths_do_not_gain_a_declared_block_channel(raises: bool, caplog: pytest.LogCaptureFixture) -> None:
    builder_calls: list[Any] = []
    failure = RuntimeError("original failure")

    def blocks(value: Any) -> list[dict[str, Any]]:
        builder_calls.append(value)
        return [{"type": "resource_link", "uri": "app://items/7", "name": "item"}]

    @post("/tool", mcp_result_blocks=blocks)
    async def tool() -> Response[dict[str, str]]:
        if raises:
            raise failure
        return Response({"error": "original failure"}, status_code=422)

    app = Litestar(route_handlers=[tool], logging_config=None)
    service = MCPHandlerService(
        config=MCPConfig(),
        discovered_tools={"tool": tool},
        discovered_resources={},
        discovered_prompts={},
        app_ref=app,
        registry=None,
    )
    result = await service.tools_call(
        {"name": "tool", "arguments": {}}, RequestContext(client_id="test", owner_id=None)
    )

    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"]) == {"error": "original failure"}
    assert len(result["content"]) == 1
    assert "structuredContent" not in result
    assert builder_calls == []
    assert not [record for record in caplog.records if record.name == "litestar_mcp.services.handler"]
