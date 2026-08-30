"""Litestar Model Context Protocol Integration Plugin.

A lightweight plugin that exposes Litestar routes as MCP tools, resources,
and prompts via JSON-RPC 2.0 over Streamable HTTP. Mark a route handler by
passing ``mcp_tool="name"``, ``mcp_resource="name"``, or
``mcp_prompt="name"`` directly to the Litestar decorator — Litestar funnels
unknown kwargs into ``handler.opt`` automatically, so no ``opt={...}``
wrapper or ``@mcp_tool`` / ``@mcp_resource`` / ``@mcp_prompt`` second
decorator is needed. The stacked decorator form is retained for parity
(useful when you need an explicit ``input_schema`` / ``output_schema``,
``annotations``, ``scopes``, or task/MRTR policy) but the kwarg form is the
recommended approach. Standalone prompts not bound to a route handler can
also be registered via ``LitestarMCP(prompts=[...])`` after decoration with
``@mcp_prompt``.
"""

from litestar_mcp.__metadata__ import __version__
from litestar_mcp.app import MCP, MCPStdioContext
from litestar_mcp.auth import (
    DefaultJWKSCache,
    JWKSCache,
    MCPAuthBackend,
    MCPAuthConfig,
    OIDCProviderConfig,
    TokenValidator,
    create_oidc_validator,
)
from litestar_mcp.config import (
    AfterToolCallHook,
    BeforeToolCallHook,
    MCPAppsConfig,
    MCPConfig,
    MCPOptKeys,
    MCPTaskConfig,
    MCPToolPolicy,
)
from litestar_mcp.content import MCPBlobResource, MCPInputRequiredResult, MCPResourceLink, MCPToolResult
from litestar_mcp.exceptions import (
    BridgeConnectionError,
    BridgeMessageTooLargeError,
    LitestarMCPError,
    MissingDependencyError,
)
from litestar_mcp.plugin import LitestarMCP
from litestar_mcp.progress import ProgressReporter, RequestNotificationStream, progress_params
from litestar_mcp.routes import MCPController
from litestar_mcp.services.handler import RETRYABLE_META_KEY, MCPRequestContext, get_mcp_request_context
from litestar_mcp.task_backends import AsyncioTaskBackend, TaskExecutionBackend, TaskInvocation
from litestar_mcp.tasks import MCPTaskStore, TaskRecord
from litestar_mcp.ui import UI_EXTENSION, UI_MIME_TYPE
from litestar_mcp.utils import mcp_prompt, mcp_resource, mcp_tool
from litestar_mcp.validation import MsgspecToolTypeAdapter, ToolTypeAdapter, ValidationIssue

__all__ = (
    "MCP",
    "RETRYABLE_META_KEY",
    "UI_EXTENSION",
    "UI_MIME_TYPE",
    "AfterToolCallHook",
    "AsyncioTaskBackend",
    "BeforeToolCallHook",
    "BridgeConnectionError",
    "BridgeMessageTooLargeError",
    "DefaultJWKSCache",
    "JWKSCache",
    "LitestarMCP",
    "LitestarMCPError",
    "MCPAppsConfig",
    "MCPAuthBackend",
    "MCPAuthConfig",
    "MCPBlobResource",
    "MCPConfig",
    "MCPController",
    "MCPInputRequiredResult",
    "MCPOptKeys",
    "MCPRequestContext",
    "MCPResourceLink",
    "MCPStdioContext",
    "MCPTaskConfig",
    "MCPTaskStore",
    "MCPToolPolicy",
    "MCPToolResult",
    "MissingDependencyError",
    "MsgspecToolTypeAdapter",
    "OIDCProviderConfig",
    "ProgressReporter",
    "RequestNotificationStream",
    "TaskExecutionBackend",
    "TaskInvocation",
    "TaskRecord",
    "TokenValidator",
    "ToolTypeAdapter",
    "ValidationIssue",
    "__version__",
    "create_oidc_validator",
    "get_mcp_request_context",
    "mcp_prompt",
    "mcp_resource",
    "mcp_tool",
    "progress_params",
)
