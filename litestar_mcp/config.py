"""Configuration for Litestar MCP Plugin."""

from collections.abc import Sequence  # noqa: TC003 - Litestar evaluates config annotations at runtime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from litestar.stores.base import Store  # noqa: TC002

from litestar_mcp.auth import MCPAuthConfig  # noqa: TC001
from litestar_mcp.task_backends import TaskExecutionBackend  # noqa: TC001
from litestar_mcp.validation import ToolTypeAdapter  # noqa: TC001 - runtime config annotation

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from litestar import Request


class BeforeToolCallHook(Protocol):
    """Callback invoked before an MCP ``tools/call`` dispatch."""

    def __call__(
        self,
        tool_name: "str",
        arguments: "dict[str, Any]",
        request: "Request[Any, Any, Any]",
        /,
    ) -> "Awaitable[None] | None":
        """Observe a tool call before guards and handler execution."""


class MCPToolPolicy(Protocol):
    """Optional request-scoped discovery and invocation policy."""

    async def transform_tools(
        self,
        tools: "list[dict[str, Any]]",
        request: "Request[Any, Any, Any]",
    ) -> "list[dict[str, Any]]":
        """Filter or transform tools visible to this request."""

    async def allows_tool(
        self,
        name: "str",
        arguments: "dict[str, Any]",
        request: "Request[Any, Any, Any]",
    ) -> "bool":
        """Authorize one call using the same policy as discovery."""


class MCPToolArgumentTransform(Protocol):
    """Optional additional policy hook: rewrite arguments before dispatch.

    Implement ``transform_arguments`` on a tool policy to inject or rewrite
    call arguments AFTER ``allows_tool`` authorizes the call and BEFORE the
    handler pipeline runs — e.g. supplying a required path parameter from the
    verified request identity that ``transform_tools`` hid from the
    advertised schema. The hook is discovered by name (``getattr``), so
    policies that do not implement it are unaffected.
    """

    async def transform_arguments(
        self,
        name: "str",
        arguments: "dict[str, Any]",
        request: "Request[Any, Any, Any]",
    ) -> "dict[str, Any]":
        """Return the arguments the handler pipeline should dispatch with."""


class MCPResourcePolicy(Protocol):
    """Optional policy hooks mirroring the tool seam for resources.

    Resources are dispatched to the same handlers as tools and need the
    same request-scoped narrowing: a URI template that carried a tenant
    would let a caller choose one, and an unauthenticated request should
    discover nothing it may not read. Implement any of these on a tool
    policy; each is discovered by name (``getattr``), so a policy that
    omits them keeps today's behaviour.

    A denied read is reported as "resource not found", matching how a
    denied tool call is reported, so refusal does not disclose existence.
    """

    async def transform_resources(
        self,
        entries: "list[dict[str, Any]]",
        request: "Request[Any, Any, Any]",
    ) -> "list[dict[str, Any]]":
        """Filter or transform resources and templates visible to a request."""

    async def allows_resource(
        self,
        uri: "str",
        arguments: "dict[str, Any]",
        request: "Request[Any, Any, Any]",
    ) -> "bool":
        """Authorize one read, using the same policy as discovery.

        ``arguments`` are the variables extracted from a URI template, or
        empty for a static resource.
        """

    async def transform_resource_arguments(
        self,
        uri: "str",
        arguments: "dict[str, Any]",
        request: "Request[Any, Any, Any]",
    ) -> "dict[str, Any]":
        """Return the arguments the resource handler should dispatch with."""


class AfterToolCallHook(Protocol):
    """Callback invoked after an MCP ``tools/call`` dispatch."""

    def __call__(
        self,
        tool_name: "str",
        arguments: "dict[str, Any]",
        request: "Request[Any, Any, Any]",
        /,
        *,
        result: "Any",
        exception: "Exception | None",
        duration: "float",
    ) -> "Awaitable[None] | None":
        """Observe a completed, failed, or rejected tool call."""


@dataclass(frozen=True)
class MCPOptKeys:
    """Configurable names for the ``handler.opt`` keys read by the plugin.

    Downstream apps can rename any key to avoid collisions with other plugins
    or app-specific conventions. All fields default to ``mcp_<purpose>`` and
    the pattern mirrors ``litestar.security.jwt.auth.JWTAuth.exclude_opt_key``.

    Attributes:
        tool: Opt key that marks a route handler as an MCP tool
            (``handler.opt[tool] = "<tool-name>"``).
        tool_result_blocks: Opt key carrying a callable that maps a
            handler's serialized response to extra MCP content blocks. A
            route serving both HTTP and MCP keeps its own response shape
            while its tool result additionally carries resource links or
            embedded resources. The callable receives the decoded response
            body, the same data a client sees.
        resource: Opt key that marks a route handler as an MCP resource.
        resource_uri: Opt key that carries a concrete resource URI override.
        resource_mime_type: Opt key that carries the resource MIME type for
            list responses and binary ``resources/read`` fallbacks.
        resource_template: Opt key that carries an RFC 6570 Level 1 URI
            template for the resource (``handler.opt[resource_template] =
            "app://workspaces/{workspace_id}/files/{file_id}"``).
        required_client_capabilities: Opt key declaring standard client
            capabilities required before a tool may be invoked.
        task_input_before_start: Opt key selecting a synchronous MRTR input
            round before task creation.
        prompt: Opt key that marks a route handler as an MCP prompt
            (``handler.opt[prompt] = "<prompt-name>"``).
        description: Opt key overriding the tool description
            (``handler.opt[description] = "LLM prose"``).
        resource_description: Opt key overriding the resource description.
            Kept distinct from ``description`` so a handler that exposes both
            a tool and a resource on the same route can target each.
        prompt_description: Opt key overriding the prompt description.
        prompt_title: Opt key overriding the prompt title.
        prompt_arguments: Opt key overriding the prompt argument list (a
            ``list[dict]`` matching the decorator's ``arguments=`` param).
        prompt_icons: Opt key overriding the prompt icons list (a
            ``list[dict]`` matching the decorator's ``icons=`` param).
        agent_instructions: Opt key for the ``## Instructions`` section.
        when_to_use: Opt key for the ``## When to use`` section.
        returns: Opt key for the ``## Returns`` section.
    """

    tool: "str" = "mcp_tool"
    tool_result_blocks: "str" = "mcp_result_blocks"
    flatten_body: "str" = "mcp_flatten_body"
    ui_resource_uri: "str" = "mcp_ui_resource_uri"
    ui_visibility: "str" = "mcp_ui_visibility"
    resource_ui: "str" = "mcp_resource_ui"
    resource: "str" = "mcp_resource"
    resource_uri: "str" = "mcp_resource_uri"
    resource_mime_type: "str" = "mcp_resource_mime_type"
    resource_template: "str" = "mcp_resource_template"
    resource_completions: "str" = "mcp_resource_completions"
    required_client_capabilities: "str" = "mcp_required_client_capabilities"
    task_input_before_start: "str" = "mcp_task_input_before_start"
    prompt: "str" = "mcp_prompt"
    description: "str" = "mcp_description"
    resource_description: "str" = "mcp_resource_description"
    prompt_description: "str" = "mcp_prompt_description"
    prompt_title: "str" = "mcp_prompt_title"
    prompt_arguments: "str" = "mcp_prompt_arguments"
    prompt_icons: "str" = "mcp_prompt_icons"
    prompt_completions: "str" = "mcp_prompt_completions"
    agent_instructions: "str" = "mcp_agent_instructions"
    when_to_use: "str" = "mcp_when_to_use"
    returns: "str" = "mcp_returns"

    def for_field(self, field_name: "str", kind: "Literal['tool', 'resource', 'prompt']") -> "str":
        """Return the opt key for ``(field_name, kind)``.

        The ``description`` field has kind-specific keys (``description`` for
        tools, ``resource_description`` for resources, ``prompt_description``
        for prompts) so a handler exposing multiple MCP roles on the same
        route can carry distinct override prose. All other fields are
        kind-agnostic.
        """
        if field_name == "description" and kind == "resource":
            return self.resource_description
        if field_name == "description" and kind == "prompt":
            return self.prompt_description
        value: str = getattr(self, field_name)
        return value


@dataclass
class MCPTaskConfig:
    """Configuration for the opt-in MCP Tasks extension."""

    store: "Store | None" = None
    default_ttl_ms: "int | None" = 300_000
    """Default lifetime from creation; ``None`` retains tasks indefinitely."""
    max_ttl_ms: "int" = 3_600_000
    poll_interval_ms: "int" = 1_000
    execution_backend: "TaskExecutionBackend | None" = None
    """Owns how created tasks execute. ``None`` selects the in-process
    asyncio runner; durable deployments supply a backend over their own
    execution engine."""

    def __post_init__(self) -> "None":
        if self.default_ttl_ms is not None and self.default_ttl_ms < 0:
            msg = "default_ttl_ms must be non-negative or None"
            raise ValueError(msg)
        if self.max_ttl_ms < 0:
            msg = "max_ttl_ms must be non-negative"
            raise ValueError(msg)
        if self.default_ttl_ms is not None and self.max_ttl_ms < self.default_ttl_ms:
            msg = "max_ttl_ms must be greater than or equal to default_ttl_ms"
            raise ValueError(msg)
        if self.poll_interval_ms <= 0:
            msg = "poll_interval_ms must be positive"
            raise ValueError(msg)


def normalize_task_config(value: "bool | MCPTaskConfig") -> "MCPTaskConfig | None":
    """Normalize task configuration into a concrete config object."""
    if value is False:
        return None
    if value is True:
        return MCPTaskConfig()
    return value


@dataclass
class MCPAppsConfig:
    """Configuration for the opt-in MCP Apps extension (SEP-1865).

    Apps are interactive resources declared under the ``ui://`` URI
    scheme. Enabling apps advertises ``io.modelcontextprotocol/ui`` with
    the ``mimeTypes`` settings in ``server/discover`` and exposes declared
    ``ui://`` resources to clients whose declared mimeTypes intersect;
    disabled, declared app resources resolve inert (hidden) rather than
    invalid.
    """

    mime_types: "tuple[str, ...]" = ("text/html;profile=mcp-app",)
    """Content types this server can deliver; SEP-1865's initial profile
    is the default and currently the only standardized value."""

    def __post_init__(self) -> "None":
        if not self.mime_types or any(not value.strip() for value in self.mime_types):
            msg = "apps mime_types must be a non-empty tuple of content types"
            raise ValueError(msg)


def normalize_apps_config(value: "bool | MCPAppsConfig") -> "MCPAppsConfig | None":
    """Normalize apps configuration into a concrete config object."""
    if value is False:
        return None
    if value is True:
        return MCPAppsConfig()
    return value


@dataclass
class MCPConfig:
    """Configuration for the Litestar MCP Plugin.

    The plugin uses Litestar's opt attribute to discover routes marked for MCP exposure.
    Server name and version are derived from the Litestar app's OpenAPI configuration.

    Attributes:
        base_path: Base path for MCP API endpoints.
        include_in_schema: Whether to include MCP routes in OpenAPI schema generation.
        name: Optional override for server name. If not set, uses OpenAPI title.
        guards: Optional list of guards to protect MCP endpoints.
        route_opt: Optional route ``opt`` mapping applied to the mounted MCP
            router. Use this to declare an opt-based authentication policy for
            the MCP surface.
        register_oauth_protected_resource: Whether to register the RFC 9728
            protected resource metadata route. Disable this when another
            plugin owns the application-root discovery path.
        register_agent_card: Whether to register the agent card discovery
            route.
        allowed_origins: Exact additional Origin values to accept. A missing
            Origin is valid; a present Origin must match the request origin or
            one of these configured values.
        auth: Optional OAuth 2.1 auth configuration. When set, bearer token validation
            is enforced on MCP endpoints.
        tasks: Optional task configuration or ``True`` to enable the default
            experimental in-memory task implementation.
        list_page_size: Page size for ``tools/list``, ``resources/list``,
            ``resources/templates/list``, and ``prompts/list``. The MCP spec
            lets servers choose the page size; clients cannot override it per
            request — they page through results via the opaque ``cursor`` /
            ``nextCursor`` round-trip. Must be a positive integer.
        before_tool_call: Optional callback invoked once before each
            ``tools/call`` dispatch, after the synthesized request is built
            and before guards run.
        after_tool_call: Optional callback invoked once after each
            ``tools/call`` dispatch with either the result or exception and
            elapsed dispatch duration in seconds.
        max_blob_bytes: Maximum raw byte length for base64-embedded MCP blobs.
            Set to ``None`` to disable the library cap.
        type_adapters: Optional first-match tool type adapter chain. ``None``
            auto-detects host integrations; msgspec is always appended as
            the terminal adapter.
        tool_policy: Optional request-scoped policy shared by tools/list and
            tools/call so discovery and execution cannot drift.
    """

    base_path: "str" = "/mcp"
    include_in_schema: "bool" = False
    name: "str | None" = None
    instructions: "str | None" = None
    guards: "list[Any] | None" = None
    allowed_origins: "list[str] | None" = None
    include_operations: "list[str] | None" = None
    exclude_operations: "list[str] | None" = None
    include_tags: "list[str] | None" = None
    exclude_tags: "list[str] | None" = None
    auth: "MCPAuthConfig | None" = None
    tasks: "bool | MCPTaskConfig" = False
    apps: "bool | MCPAppsConfig" = False
    type_adapters: "Sequence[ToolTypeAdapter] | None" = None
    tool_policy: "MCPToolPolicy | None" = None
    opt_keys: "MCPOptKeys" = field(default_factory=MCPOptKeys)
    cache_ttl_ms: "int" = 0
    cache_scope: "Literal['private', 'public']" = "private"
    subscription_max_streams: "int" = 10_000
    subscription_keepalive_seconds: "float" = 15.0
    subscription_channels: "Any | None" = None
    list_page_size: "int" = 100
    before_tool_call: "BeforeToolCallHook | None" = None
    after_tool_call: "AfterToolCallHook | None" = None
    max_blob_bytes: "int | None" = 25 * 1024 * 1024
    route_opt: "dict[str, Any] | None" = None
    register_oauth_protected_resource: "bool" = True
    register_agent_card: "bool" = True

    def __post_init__(self) -> "None":
        if self.list_page_size <= 0:
            msg = f"list_page_size must be a positive integer, got {self.list_page_size}"
            raise ValueError(msg)
        if self.max_blob_bytes is not None and self.max_blob_bytes < 0:
            msg = f"max_blob_bytes must be non-negative or None, got {self.max_blob_bytes}"
            raise ValueError(msg)
        if self.cache_ttl_ms < 0:
            msg = f"cache_ttl_ms must be non-negative, got {self.cache_ttl_ms}"
            raise ValueError(msg)
        if self.subscription_max_streams <= 0:
            msg = f"subscription_max_streams must be positive, got {self.subscription_max_streams}"
            raise ValueError(msg)
        if self.subscription_keepalive_seconds <= 0:
            msg = f"subscription_keepalive_seconds must be positive, got {self.subscription_keepalive_seconds}"
            raise ValueError(msg)

    @property
    def apps_config(self) -> "MCPAppsConfig | None":
        """The normalized apps configuration, or ``None`` when disabled."""
        return normalize_apps_config(self.apps)

    @property
    def task_config(self) -> "MCPTaskConfig | None":
        """Return the normalized task configuration, if task support is enabled."""
        return normalize_task_config(self.tasks)
