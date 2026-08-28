"""Registry-driven MCP completion capability and dispatch."""

from typing import Any

import pytest
from litestar import Litestar, get
from litestar.testing import AsyncTestClient

from litestar_mcp import LitestarMCP, MCPConfig
from litestar_mcp.utils import mcp_prompt

pytestmark = pytest.mark.integration


@pytest.fixture
def anyio_backend() -> "str":
    return "asyncio"


async def _rpc(
    client: "AsyncTestClient[Any]",
    method: "str",
    params: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    return response.json()  # type: ignore[no-any-return]


def _completion_app(calls: "list[tuple[str, str, dict[str, str]]]") -> "Litestar":
    def styles(value: "str", arguments: "dict[str, str]") -> "list[str]":
        calls.append(("prompt", value, arguments))
        return [f"{value}-{arguments['locale']}", "formal"]

    async def cities(value: "str", arguments: "dict[str, str]") -> "list[str]":
        calls.append(("resource", value, arguments))
        return [f"{value}-{index}" for index in range(105)]

    @mcp_prompt(
        name="draft",
        arguments=[{"name": "style"}, {"name": "locale"}],
        completions={"style": styles},
    )
    def draft(style: "str", locale: "str") -> "str":
        return f"{style}:{locale}"

    @get(
        "/places/{city:str}",
        mcp_resource="places",
        mcp_resource_template="places://{country}/{city}",
        mcp_resource_completions={"city": cities},
        sync_to_thread=False,
    )
    def places(city: "str") -> "dict[str, str]":
        return {"city": city}

    return Litestar(
        route_handlers=[places],
        plugins=[LitestarMCP(MCPConfig(), prompts=[draft])],
    )


@pytest.mark.anyio
async def test_discover_advertises_completions_only_when_a_provider_exists() -> "None":
    calls: list[tuple[str, str, dict[str, str]]] = []
    with_completer = _completion_app(calls)
    without_completer = Litestar(plugins=[LitestarMCP(MCPConfig())])

    async with AsyncTestClient(app=with_completer) as client:
        enabled = await _rpc(client, "server/discover")
    async with AsyncTestClient(app=without_completer) as client:
        disabled = await _rpc(client, "server/discover")

    assert enabled["result"]["capabilities"]["completions"] == {}
    assert "completions" not in disabled["result"]["capabilities"]


@pytest.mark.anyio
async def test_prompt_completer_receives_value_and_context_arguments() -> "None":
    calls: list[tuple[str, str, dict[str, str]]] = []
    async with AsyncTestClient(app=_completion_app(calls)) as client:
        response = await _rpc(
            client,
            "completion/complete",
            {
                "ref": {"type": "ref/prompt", "name": "draft"},
                "argument": {"name": "style", "value": "brief"},
                "context": {"arguments": {"locale": "en-GB"}},
            },
        )

    assert response["result"]["completion"] == {
        "values": ["brief-en-GB", "formal"],
        "total": 2,
        "hasMore": False,
    }
    assert calls == [("prompt", "brief", {"locale": "en-GB"})]


@pytest.mark.anyio
async def test_async_resource_completer_is_capped_at_one_hundred_values() -> "None":
    calls: list[tuple[str, str, dict[str, str]]] = []
    async with AsyncTestClient(app=_completion_app(calls)) as client:
        response = await _rpc(
            client,
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "places://{country}/{city}"},
                "argument": {"name": "city", "value": "lon"},
                "context": {"arguments": {"country": "gb"}},
            },
        )

    completion = response["result"]["completion"]
    assert completion["values"] == [f"lon-{index}" for index in range(100)]
    assert completion["total"] == 105
    assert completion["hasMore"] is True
    assert calls == [("resource", "lon", {"country": "gb"})]


@pytest.mark.anyio
async def test_unknown_ref_or_argument_is_invalid_params() -> "None":
    calls: list[tuple[str, str, dict[str, str]]] = []
    async with AsyncTestClient(app=_completion_app(calls)) as client:
        unknown_ref = await _rpc(
            client,
            "completion/complete",
            {
                "ref": {"type": "ref/prompt", "name": "missing"},
                "argument": {"name": "style", "value": ""},
            },
        )
        unknown_argument = await _rpc(
            client,
            "completion/complete",
            {
                "ref": {"type": "ref/prompt", "name": "draft"},
                "argument": {"name": "missing", "value": ""},
            },
        )

    assert unknown_ref["error"]["code"] == -32602
    assert unknown_argument["error"]["code"] == -32602
    assert calls == []
