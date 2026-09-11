=====
Tools
=====

Tools are executable operations — anything that takes arguments and
returns structured output. Tag a Litestar route handler with
``mcp_tool="<tool_name>"`` and the plugin publishes it via ``tools/list``
and ``tools/call``. The task-manager demo registers five tool handlers
covering the full CRUD lifecycle:

.. literalinclude:: /examples/task_manager/main.py
    :language: python
    :caption: ``docs/examples/task_manager/main.py`` - ``register_tools``
    :pyobject: register_tools

The handlers themselves are ordinary Litestar ``@get`` / ``@post`` /
``@delete`` callables — the only extra is the ``mcp_tool`` kwarg. Each
tool is discoverable via ``tools/list`` and invocable via
``tools/call``.

Tool arguments are validated against the handler's ``parsed_fn_signature``
before dispatch — the same model Litestar uses for ordinary HTTP request
parsing. Missing required arguments surface as JSON-RPC
``INVALID_PARAMS`` (``-32602``). ``Annotated[T, Parameter(...)]`` query
arguments are unwrapped and their ``Parameter`` constraints
(``ge`` / ``le`` / ``min_length`` / ``pattern`` / …) flow through into
the advertised ``inputSchema``.

Tool Hints
==========

Declare behavioral hints through :func:`~litestar_mcp.mcp_tool` using optional
``bool | None`` parameters:

.. literalinclude:: /examples/snippets/tool_hints.py
    :language: python
    :caption: ``docs/examples/snippets/tool_hints.py``
    :start-after: # start-example
    :end-before: # end-example
    :dedent:

The equivalent route kwargs (or entries in ``opt={...}``) are
``mcp_read_only_hint``, ``mcp_destructive_hint``, ``mcp_idempotent_hint``, and
``mcp_open_world_hint``. Rename them with the corresponding
``MCPOptKeys.read_only_hint``, ``destructive_hint``, ``idempotent_hint``, and
``open_world_hint`` fields. Standalone ``@mcp.tool(...)`` accepts these same
route opt keys.

Clients consume these values in ``tools/list`` under
``tools[].annotations.readOnlyHint``, ``destructiveHint``, ``idempotentHint``,
and ``openWorldHint``. No defaults are materialized: omitted hints remain
absent, and explicit ``False`` values are preserved. Boolean route opt values
win over typed decorator hints, which win over the same keys in the existing
``annotations=`` dictionary. Non-boolean route opt values (including ``None``)
are ignored, not coerced. Other annotations and scopes are retained; the
provided annotations dictionary is not mutated. These are advisory metadata,
not authorization or execution policy.

Explicit Input Schemas
======================

Use :func:`~litestar_mcp.mcp_tool` with ``input_schema=`` when the generated
Litestar schema cannot express the exact client-facing JSON Schema 2020-12
contract. The explicit schema replaces inference for discovery and bridge
header generation, so keep it aligned with the handler's actual validation:

.. literalinclude:: /examples/snippets/tool_explicit_input_schema.py
    :language: python
    :caption: ``docs/examples/snippets/tool_explicit_input_schema.py``
    :start-after: # start-example
    :end-before: # end-example
    :dedent:

Task Input Before Execution
===========================

For a task-capable tool that must complete an MRTR input round before task
creation, combine ``task_support`` with ``task_input_before_start=True``:

.. literalinclude:: /examples/snippets/tool_task_input_before_start.py
    :language: python
    :caption: ``docs/examples/snippets/tool_task_input_before_start.py``
    :start-after: # start-example
    :end-before: # end-example
    :dedent:

The first response is synchronous ``input_required`` and has no task ID. A
retry carrying ``inputResponses`` may create the task. Integrity-protect
authorization-sensitive ``requestState`` and bind it to the authenticated
principal, original arguments, and an expiry.

JSON-RPC Round-Trip
===================

Clients drive tools with independent ``tools/list`` and ``tools/call`` POST
requests. Every request includes the 2026-07-28 metadata envelope and matching
HTTP routing headers:

.. code-block:: bash

    # List every tool marked in the application
    curl -sS -X POST http://localhost:8000/mcp \
      -H "Content-Type: application/json" \
      -H "MCP-Protocol-Version: 2026-07-28" \
      -H "Mcp-Method: tools/list" \
      -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'

    # Execute a specific tool (task-manager demo)
    curl -sS -X POST http://localhost:8000/mcp \
      -H "Content-Type: application/json" \
      -H "MCP-Protocol-Version: 2026-07-28" \
      -H "Mcp-Method: tools/call" \
      -H "Mcp-Name: list_tasks" \
      -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
           "params":{"name":"list_tasks","arguments":{},
           "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
           "io.modelcontextprotocol/clientCapabilities":{}}}}'

Successful responses carry the handler's return value inside the
standard JSON-RPC envelope.

Binary and Mixed Content Results
================================

For ordinary JSON-like return values, the plugin keeps the existing behavior:
the value is serialized into a text content block. When a tool needs to return
MCP content blocks directly, use the public helper types:

.. literalinclude:: /examples/snippets/tool_binary_content.py
    :language: python
    :caption: ``docs/examples/snippets/tool_binary_content.py``
    :start-after: # start-example
    :end-before: # end-example
    :dedent:

Use :class:`~litestar_mcp.MCPResourceLink` when the client should fetch bytes
later with ``resources/read``. Use :class:`~litestar_mcp.MCPBlobResource` only
when the bytes need to be embedded immediately in the JSON-RPC response. A
handler returning raw ``bytes`` directly is treated like an ordinary handler
return value, not as an implicit blob.

Specification Results and Lifecycle Hooks
========================================

A structural specification ``CallToolResult`` (with ``model_dump``, ``content``,
``structured_content``, and ``is_error`` attributes) is forwarded intact,
including its ``isError``, ``structuredContent``, and ``_meta`` fields. It still
bypasses HTTP response shaping, including ``after_request`` and type encoders.
The executor drives one synthetic response through Litestar's ``before_send``
hooks before ``after_response`` and ``after_tool_call`` observers run. That response has
an empty body and status 200 for success or 500 for ``is_error=True``, allowing
status-sensitive transaction hooks to distinguish commit from rollback. The
synthetic status is not substituted for the specification result.

Required send-hook failures follow the existing exception-handler and terminal
error-cleanup path; they are not swallowed as observer failures. Unhandled
failures reach ``after_tool_call`` as the original exception, even if fallback
cleanup also fails. As with ordinary responses, failure recovery may send a
separate terminal error response. A returned specification result, including
``isError=True``, still reaches ``after_tool_call`` as ``result`` with
``exception=None``.

Declared Result Blocks
======================

A route can keep its HTTP response shape while declaring extra MCP content
blocks with ``mcp_result_blocks=builder`` (remappable through
``MCPOptKeys.tool_result_blocks``). For ordinary handler returns, the synchronous
builder receives the decoded response body. Explicit specification/helper
results use their own content blocks. If the builder raises, the result is
returned without the additional blocks. The ``litestar_mcp.services.handler``
logger emits one warning identifying only the tool and configured opt key,
each limited to 128 ASCII identifier characters. It logs no result, arguments,
exception text, or traceback. This
is additive result decoration, not an error-result block channel.

Error Contract
==============

Tool errors are reported differently from the other primitives. A handler
that raises or returns an error response is surfaced **inside the tool
result** with ``isError: true`` (and the detail in ``content``), not as a
JSON-RPC ``error`` object — this lets the model see and react to the
failure. Only protocol-level problems (an unknown tool name, malformed
params) use a JSON-RPC ``error`` with ``INVALID_PARAMS`` (``-32602``).
See :doc:`prompts` and :doc:`resources` for how those primitives map the
handler's HTTP status onto JSON-RPC codes.
