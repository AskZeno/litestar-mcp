"""Shared tool type-adapter validation and schema ownership."""

import json
from typing import Annotated, Any, cast
from uuid import UUID

import msgspec
from litestar import Litestar, get, post
from litestar.testing import TestClient
from pydantic import BaseModel, Field

from litestar_mcp import (
    LitestarMCP,
    MCPConfig,
    MsgspecToolTypeAdapter,
    ValidationIssue,
)
from litestar_mcp.contrib.pydantic import PydanticToolTypeAdapter
from litestar_mcp.schema_builder import generate_schema_for_handler

PROTOCOL_VERSION = "2026-07-28"


def _rpc(client: TestClient[Any], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    request_params = dict(params or {})
    request_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "adapter-tests", "version": "1"},
    }
    headers = {
        "Accept": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if method == "tools/call":
        headers["Mcp-Name"] = str(request_params.get("name", ""))
    return cast(
        "dict[str, Any]",
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": request_params},
            headers=headers,
        ).json(),
    )


def _tool(app: Litestar, name: str) -> dict[str, Any]:
    with TestClient(app=app) as client:
        listed = _rpc(client, "tools/list")["result"]["tools"]
    return cast("dict[str, Any]", next(tool for tool in listed if tool["name"] == name))


def _handler(app: Litestar, fn: Any) -> Any:
    return next(
        route_handler
        for route in app.routes
        for route_handler in getattr(route, "route_handlers", ())
        if getattr(route_handler, "fn", None) is getattr(fn, "fn", fn)
    )


def test_pydantic_body_auto_detects_validation_and_real_json_schema() -> None:
    class Address(BaseModel):
        zip_code: int = Field(ge=1)

    class Payload(BaseModel):
        request_id: UUID
        address: Address

    @post("/submit", mcp_tool="submit", sync_to_thread=False)
    def submit(data: Annotated[Payload, Field(title="Payload")]) -> dict[str, Any]:
        return data.model_dump(mode="json")

    plugin = LitestarMCP(MCPConfig())
    app = Litestar(route_handlers=[submit], plugins=[plugin])

    assert isinstance(plugin.type_adapters[0], PydanticToolTypeAdapter)
    assert isinstance(plugin.type_adapters[-1], MsgspecToolTypeAdapter)

    tool = _tool(app, "submit")
    input_schema = tool["inputSchema"]
    data_schema = input_schema["properties"]["data"]
    assert data_schema["$ref"] == "#/$defs/Payload"
    payload_schema = input_schema["$defs"]["Payload"]
    assert payload_schema["type"] == "object"
    assert payload_schema["properties"]["request_id"] == {
        "format": "uuid",
        "title": "Request Id",
        "type": "string",
    }
    assert payload_schema["properties"]["address"] == {"$ref": "#/$defs/Address"}
    assert input_schema["$defs"]["Address"]["properties"]["zip_code"]["minimum"] == 1

    request_id = "12345678-1234-5678-1234-567812345678"
    with TestClient(app=app) as client:
        accepted = _rpc(
            client,
            "tools/call",
            {
                "name": "submit",
                "arguments": {"data": {"request_id": request_id, "address": {"zip_code": 42}}},
            },
        )
        rejected = _rpc(
            client,
            "tools/call",
            {
                "name": "submit",
                "arguments": {"data": {"request_id": request_id, "address": {"zip_code": "bad"}}},
            },
        )

    assert accepted["result"]["isError"] is False
    assert json.loads(accepted["result"]["content"][0]["text"])["request_id"] == request_id
    error = json.loads(rejected["result"]["content"][0]["text"])
    assert error["error"] == "Invalid tool arguments"
    assert any(issue["path"] == "/arguments/data/address/zip_code" for issue in error["errors"])


def test_uuid_scalar_schema_matches_the_accepted_wire_value() -> None:
    @get("/lookup", mcp_tool="lookup", sync_to_thread=False)
    def lookup(item_id: UUID) -> dict[str, str]:
        return {"item_id": str(item_id)}

    schema = _tool(Litestar(route_handlers=[lookup], plugins=[LitestarMCP(MCPConfig())]), "lookup")["inputSchema"]
    assert schema["properties"]["item_id"] == {"type": "string", "format": "uuid"}


def test_explicit_adapter_precedes_the_msgspec_terminal() -> None:
    class UpperStringAdapter:
        def supports_type(self, annotation: Any) -> bool:
            return annotation is str

        def validate(self, value: Any, _annotation: Any) -> list[ValidationIssue]:
            if isinstance(value, str) and value.isupper():
                return []
            return [ValidationIssue(message="Value must be uppercase")]

        def json_schema(self, _annotation: Any) -> dict[str, Any] | None:
            return {"type": "string", "format": "uppercase"}

    @get("/shout", mcp_tool="shout", sync_to_thread=False)
    def shout(value: str) -> dict[str, str]:
        return {"value": value}

    plugin = LitestarMCP(MCPConfig(type_adapters=[UpperStringAdapter()]))
    app = Litestar(route_handlers=[shout], plugins=[plugin])
    assert isinstance(plugin.type_adapters[-1], MsgspecToolTypeAdapter)
    assert _tool(app, "shout")["inputSchema"]["properties"]["value"] == {
        "type": "string",
        "format": "uppercase",
    }

    with TestClient(app=app) as client:
        rejected = _rpc(client, "tools/call", {"name": "shout", "arguments": {"value": "quiet"}})
    error = json.loads(rejected["result"]["content"][0]["text"])
    assert error["errors"] == [{"path": "/arguments/value", "message": "Value must be uppercase"}]


def test_msgspec_struct_schema_is_identical_with_the_auto_detected_chain() -> None:
    class Point(msgspec.Struct):
        x: int
        y: int

    @get("/point", mcp_tool="point", sync_to_thread=False)
    def point(value: Point) -> dict[str, int]:
        return {"x": value.x, "y": value.y}

    plugin = LitestarMCP(MCPConfig())
    app = Litestar(route_handlers=[point], plugins=[plugin])
    handler = _handler(app, point)

    assert generate_schema_for_handler(handler, plugin.type_adapters) == generate_schema_for_handler(handler)
