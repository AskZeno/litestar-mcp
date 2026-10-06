"""Primitive-aware MCP JSON-RPC error helpers.

Error contract. The JSON-RPC ``error.code`` reflects the *primitive-
level* error class defined by the MCP spec, **not** the handler's HTTP status:

* ``resources/read`` unknown URI -> ``-32002`` (spec-mandated "Resource not found").
* ``resources/read`` handler error (any status) -> ``-32603`` Internal error.
* ``prompts/get`` unknown name / missing / invalid args -> ``-32602`` Invalid params
  (raised pre-execution in ``routes.py``).
* ``prompts/get`` handler execution error (any status) -> ``-32603`` Internal error.
* ``tools/call`` handler error -> no JSON-RPC error object; an ``isError=True``
  result envelope per the tools spec.
* An unmapped handler exception (neither an ``MCPToolErrorResult`` nor a
  Litestar ``HTTPException``) never answers with its text, which can carry
  internals: it is logged at ERROR under a fresh reference and the caller
  reads only ``The tool failed. Reference: <reference>`` (the ``isError``
  content of a tool; ``error.data.content`` with ``statusCode`` 500 for a
  resource or prompt).

The handler's real HTTP status is never dropped: it is preserved in
``error.data.statusCode`` so clients can recover the finer signal without the
server minting non-standard JSON-RPC codes. MCP defines no codes for
401/403/409/429, so none are invented here (this deliberately supersedes
status->code mapping proposals).

RESOURCE_NOT_FOUND is the Spec-mandated resources/read "Resource not found" code
(MCP 2025-06-18, Resources §Error Handling). Note: future spec updates may migrate
this to -32602 (Invalid params).
"""

from typing import TYPE_CHECKING, Any

from litestar_mcp._unmapped import UNMAPPED_STATUS, unmapped_content
from litestar_mcp.executor import MCPToolErrorResult
from litestar_mcp.jsonrpc import INTERNAL_ERROR, JSONRPCError

if TYPE_CHECKING:
    from litestar.exceptions import HTTPException

RESOURCE_NOT_FOUND = -32602


def _tool_error_data(err: "MCPToolErrorResult") -> "dict[str, Any]":
    return {"statusCode": err.status_code, "content": err.content}


def _http_refusal_data(err: "HTTPException") -> "dict[str, Any]":
    # A Litestar HTTPException's detail is written for the client.
    return {"error": type(err).__name__, "detail": str(err), "statusCode": err.status_code}


def mcp_error_for_unmapped(message: "str", reference: "str") -> "JSONRPCError":
    """Answer an unmapped exception logged under ``reference`` without its text.

    ``error.data`` has the shape of a tool error result (``statusCode`` and
    ``content``), whose only text is the reference sentence.
    """
    return JSONRPCError(
        code=INTERNAL_ERROR,
        message=message,
        data={"statusCode": UNMAPPED_STATUS, "content": unmapped_content(reference)},
    )


def mcp_error_for_resource_content(err: "ValueError") -> "JSONRPCError":
    """Map a refusal to encode a resource's response (the blob-size cap) to an internal JSON-RPC error.

    The text is this library's own message, never the handler's.
    """
    return JSONRPCError(
        code=INTERNAL_ERROR,
        message="Resource read failed",
        data={"error": type(err).__name__, "detail": str(err)},
    )


def mcp_error_for_prompt_refusal(err: "HTTPException") -> "JSONRPCError":
    """Map a prompt handler's Litestar ``HTTPException`` to an internal JSON-RPC error."""
    return JSONRPCError(
        code=INTERNAL_ERROR,
        message="Prompt execution failed",
        data=_http_refusal_data(err),
    )


def mcp_error_for_prompt_execution(err: "MCPToolErrorResult") -> "JSONRPCError":
    """Map prompt handler execution failures to an internal JSON-RPC error."""
    return JSONRPCError(
        code=INTERNAL_ERROR,
        message="Prompt execution failed",
        data=_tool_error_data(err),
    )


def mcp_error_for_resource_not_found(uri: "str") -> "JSONRPCError":
    """Return the MCP resources/read not-found error."""
    return JSONRPCError(
        code=RESOURCE_NOT_FOUND,
        message="Resource not found",
        data={"uri": uri},
    )


def mcp_error_for_resource_read(err: "MCPToolErrorResult | HTTPException") -> "JSONRPCError":
    """Map a resource handler's error result or Litestar ``HTTPException`` to an internal JSON-RPC error.

    An unmapped exception never reaches here: it answers through
    :func:`mcp_error_for_unmapped`, without its text.
    """
    data = _tool_error_data(err) if isinstance(err, MCPToolErrorResult) else _http_refusal_data(err)
    return JSONRPCError(
        code=INTERNAL_ERROR,
        message="Resource read failed",
        data=data,
    )
