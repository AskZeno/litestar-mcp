"""Litestar MCP Plugin implementation."""

import logging
from typing import TYPE_CHECKING, Any

from litestar import Litestar, Request, Router
from litestar import get as litestar_get
from litestar.di import Provide
from litestar.handlers import BaseRouteHandler
from litestar.plugins import CLIPlugin, InitPluginProtocol

from litestar_mcp.cli import mcp_group
from litestar_mcp.config import MCPConfig
from litestar_mcp.manifests import build_agent_card, build_oauth_protected_resource
from litestar_mcp.registry import PromptRegistration, Registry
from litestar_mcp.routes import MCPController
from litestar_mcp.schema_builder import generate_schema_for_handler, validate_mcp_header_schema
from litestar_mcp.sse import SubscriptionManager
from litestar_mcp.task_backends import AsyncioTaskBackend, TaskExecutionBackend
from litestar_mcp.tasks import MCPTaskStore, TaskRecord
from litestar_mcp.ui import UI_URI_SCHEME
from litestar_mcp.utils import get_handler_function, get_mcp_metadata

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from click import Group
    from litestar.config.app import AppConfig


class LitestarMCP(InitPluginProtocol, CLIPlugin):
    """Litestar plugin for Model Context Protocol integration."""

    def __init__(
        self,
        config: "MCPConfig | None" = None,
        prompts: "Sequence[Callable[..., Any]] | None" = None,
    ) -> "None":
        """Initialize the MCP plugin.

        Args:
            config: Plugin configuration. Defaults to ``MCPConfig()``.
            prompts: Optional sequence of standalone prompt functions
                decorated with ``@mcp_prompt``. These are registered
                immediately and made available via ``prompts/list`` and
                ``prompts/get``.
        """
        self._config = config or MCPConfig()
        self._registry = Registry()
        self._dynamic_handlers: list[BaseRouteHandler] = []
        if prompts:
            for fn in prompts:
                metadata = get_mcp_metadata(fn) or {}
                if metadata.get("type") != "prompt":
                    msg = f"Function {fn!r} is not decorated with @mcp_prompt"
                    raise ValueError(msg)
                self._registry.register_prompt(
                    name=metadata["name"],
                    fn=fn,
                    title=metadata.get("title"),
                    description=metadata.get("description"),
                    arguments=metadata.get("arguments"),
                    icons=metadata.get("icons"),
                    completions=metadata.get("completions"),
                )
        self._subscription_manager = SubscriptionManager(
            max_streams=self._config.subscription_max_streams,
            channels=self._config.subscription_channels,
        )
        self._task_store: MCPTaskStore | None = None
        self._task_backend: TaskExecutionBackend | None = None
        if self._config.task_config is not None:
            task_config = self._config.task_config
            self._task_store = MCPTaskStore(
                store=task_config.store,
                default_ttl_ms=task_config.default_ttl_ms,
                max_ttl_ms=task_config.max_ttl_ms,
                poll_interval_ms=task_config.poll_interval_ms,
            )
            self._task_backend = task_config.execution_backend or AsyncioTaskBackend()
            self._task_backend.bind(self._task_store)

    @property
    def config(self) -> "MCPConfig":
        """Get the plugin configuration."""
        return self._config

    @property
    def registry(self) -> "Registry":
        """Get the central registry."""
        return self._registry

    @property
    def task_store(self) -> "MCPTaskStore | None":
        """Get the task store."""
        return self._task_store

    @property
    def task_backend(self) -> "TaskExecutionBackend | None":
        """Get the task execution backend."""
        return self._task_backend

    @property
    def discovered_tools(self) -> "dict[str, BaseRouteHandler]":
        """Get discovered MCP tools."""
        return self._registry.tools

    @property
    def discovered_resources(self) -> "dict[str, BaseRouteHandler]":
        """Get discovered MCP resources."""
        return self._registry.resources

    @property
    def discovered_prompts(self) -> "dict[str, PromptRegistration]":
        """Get discovered MCP prompts."""
        return self._registry.prompts

    def register_dynamic_handler(self, handler: "BaseRouteHandler") -> "None":
        """Register a dynamic route handler on the plugin.

        This is typically used by the wrapper class to register decorated
        tools and resources.
        """
        self._dynamic_handlers.append(handler)

    def on_cli_init(self, cli: "Group") -> "None":
        """Configure CLI commands for MCP operations."""
        cli.add_command(mcp_group)

    def on_app_init(self, app_config: "AppConfig") -> "AppConfig":
        """Initialize the MCP integration when the Litestar app starts."""
        app_config.route_handlers.extend(self._dynamic_handlers)
        self._discover_mcp_routes(app_config.route_handlers)
        self._registry.set_subscription_manager(self._subscription_manager)

        if self._task_store is not None:

            async def publish_task_status(record: "TaskRecord") -> "None":
                await self._registry.publish_notification(
                    "notifications/tasks",
                    record.to_dict(),
                )

            self._task_store.set_status_callback(publish_task_status)

        def provide_mcp_config() -> "MCPConfig":
            return self._config

        def provide_registry() -> "Registry":
            return self._registry

        def provide_task_store() -> "MCPTaskStore | None":
            return self._task_store

        def provide_task_backend() -> "TaskExecutionBackend | None":
            return self._task_backend

        router_kwargs: dict[str, Any] = {
            "path": self._config.base_path,
            "route_handlers": [MCPController],
            "tags": ["mcp"],
            "include_in_schema": self._config.include_in_schema,
            "dependencies": {
                "config": Provide(provide_mcp_config, sync_to_thread=False),
                "registry": Provide(provide_registry, sync_to_thread=False),
                "task_store": Provide(provide_task_store, sync_to_thread=False),
                "task_backend": Provide(provide_task_backend, sync_to_thread=False),
                "discovered_tools": Provide(lambda: self._registry.tools, sync_to_thread=False),
                "discovered_resources": Provide(lambda: self._registry.resources, sync_to_thread=False),
                "discovered_prompts": Provide(lambda: self._registry.prompts, sync_to_thread=False),
            },
        }
        if self._config.guards is not None:
            router_kwargs["guards"] = self._config.guards
        if self._config.route_opt is not None:
            router_kwargs["opt"] = dict(self._config.route_opt)

        mcp_router = Router(**router_kwargs)
        app_config.route_handlers.append(mcp_router)
        app_config.on_startup.append(self.on_startup)
        app_config.on_shutdown.append(self.on_shutdown)

        @litestar_get(
            "/.well-known/oauth-protected-resource",
            sync_to_thread=False,
            include_in_schema=self._config.include_in_schema,
            opt={"exclude_from_auth": True},
        )
        def oauth_protected_resource(request: "Request[Any, Any, Any]") -> "dict[str, Any]":
            return build_oauth_protected_resource(self._config.auth, request.app)

        @litestar_get(
            "/.well-known/agent-card.json",
            sync_to_thread=False,
            include_in_schema=self._config.include_in_schema,
            opt={"exclude_from_auth": True},
        )
        def agent_card(request: "Request[Any, Any, Any]") -> "dict[str, Any]":
            return build_agent_card(
                base_url=str(request.base_url),
                config=self._config,
                app=request.app,
                discovered_tools=self._registry.tools,
            )

        if self._config.register_oauth_protected_resource:
            app_config.route_handlers.append(oauth_protected_resource)
        if self._config.register_agent_card:
            app_config.route_handlers.append(agent_card)
        return app_config

    def on_startup(self, app: "Litestar") -> "None":
        """Perform discovery after app is fully initialized and routes are built."""
        all_handlers: list[BaseRouteHandler] = []
        for route in app.routes:
            if hasattr(route, "route_handlers"):
                all_handlers.extend(route.route_handlers)  # pyright: ignore[reportAttributeAccessIssue]
        _logger.debug("Plugin on_startup executing...")
        self._subscription_manager.start()
        self._discover_mcp_routes(all_handlers)
        for handler in self._registry.tools.values():
            validate_mcp_header_schema(generate_schema_for_handler(handler))
        if self._config.apps_config is not None:
            self._validate_ui_contract()

        def invalidate_router() -> "None":
            _logger.debug("invalidate_router callback triggered")
            if hasattr(app.state, "mcp_router"):
                _logger.debug("Deleting mcp_router from app state")
                delattr(app.state, "mcp_router")

        self._registry.register_change_callback(invalidate_router)
        app.state.mcp_router_invalidation_callback = invalidate_router
        _logger.debug("Registered invalidate_router callback on registry: %s", id(self._registry))

    async def on_shutdown(self, app: "Litestar") -> "None":
        """Clean up resources on application shutdown."""
        _logger.debug("Plugin on_shutdown executing...")
        callback = getattr(app.state, "mcp_router_invalidation_callback", None)
        if callback is not None:
            self._registry.unregister_change_callback(callback)
            delattr(app.state, "mcp_router_invalidation_callback")
            _logger.debug("Unregistered invalidate_router callback from registry")
        await self._subscription_manager.close_all()
        if self._task_backend is not None:
            await self._task_backend.close()

    def _validate_ui_contract(self) -> "None":
        """SEP-1865 startup validation: linkage resolves, ui:// carries the profile.

        Fails app startup loudly — a ui-linked tool whose template does not
        exist, or a ``ui://`` resource outside the configured content types,
        is a defect no request should ever observe.
        """
        from litestar_mcp.services.handler import _resource_mime_type, _resource_uri

        apps_config = self._config.apps_config
        if apps_config is None:  # pragma: no cover - guarded by the caller
            return
        declared: dict[str, str] = {}
        for name, handler in self._registry.resources.items():
            uri = _resource_uri(name, handler, self._config)
            if uri.startswith(UI_URI_SCHEME):
                declared[uri] = _resource_mime_type(handler, self._config, uri)
        for entry in self._registry.templates.values():
            if entry.template.startswith(UI_URI_SCHEME):
                declared[entry.template] = _resource_mime_type(entry.handler, self._config, entry.template)
        allowed = set(apps_config.mime_types)
        for uri, mime_type in declared.items():
            if mime_type not in allowed:
                msg = (
                    f"ui resource {uri!r} declares mimeType {mime_type!r}; SEP-1865 requires one of {sorted(allowed)!r}"
                )
                raise ValueError(msg)
        for name, handler in self._registry.tools.items():
            fn = get_handler_function(handler)
            metadata = get_mcp_metadata(handler) or get_mcp_metadata(fn) or {}
            opt = getattr(handler, "opt", None) or {}
            resource_uri = metadata.get("ui_resource_uri") or opt.get(self._config.opt_keys.ui_resource_uri)
            if resource_uri is None:
                continue
            if not isinstance(resource_uri, str) or not resource_uri.startswith(UI_URI_SCHEME):
                msg = f"tool {name!r} declares ui resource {resource_uri!r}; it must use the ui:// scheme"
                raise ValueError(msg)
            if resource_uri not in declared:
                msg = f"tool {name!r} links ui resource {resource_uri!r}, which is not declared by this server"
                raise ValueError(msg)

    def _discover_mcp_routes(self, route_handlers: "Sequence[Any]") -> "None":
        """Discover routes marked for MCP exposure via opt attribute or decorators."""
        for handler in route_handlers:
            if isinstance(handler, BaseRouteHandler):
                metadata = get_mcp_metadata(handler)
                if not metadata:
                    metadata = get_mcp_metadata(get_handler_function(handler))

                if metadata:
                    if metadata["type"] == "tool":
                        self._registry.register_tool(metadata["name"], handler)
                    elif metadata["type"] == "resource":
                        self._registry.register_resource(metadata["name"], handler)
                        template = metadata.get("resource_template")
                        if template is not None:
                            self._registry.register_resource_template(
                                metadata["name"],
                                handler,
                                template,
                                completions=metadata.get("completions"),
                            )
                    elif metadata["type"] == "prompt":
                        self._registry.register_prompt_handler(
                            metadata["name"],
                            handler,
                            title=metadata.get("title"),
                            description=metadata.get("description"),
                            arguments=metadata.get("arguments"),
                            icons=metadata.get("icons"),
                            completions=metadata.get("completions"),
                        )
                elif handler.opt:
                    tool_key = self._config.opt_keys.tool
                    resource_key = self._config.opt_keys.resource
                    template_key = self._config.opt_keys.resource_template
                    prompt_key = self._config.opt_keys.prompt
                    if tool_key in handler.opt:
                        self._registry.register_tool(handler.opt[tool_key], handler)
                    if resource_key in handler.opt:
                        resource_name = handler.opt[resource_key]
                        self._registry.register_resource(resource_name, handler)
                        opt_template = handler.opt.get(template_key)
                        if isinstance(opt_template, str):
                            self._registry.register_resource_template(
                                resource_name,
                                handler,
                                opt_template,
                                completions=handler.opt.get(self._config.opt_keys.resource_completions),
                            )
                    if prompt_key in handler.opt:
                        opt_keys = self._config.opt_keys
                        self._registry.register_prompt_handler(
                            handler.opt[prompt_key],
                            handler,
                            title=handler.opt.get(opt_keys.prompt_title),
                            description=handler.opt.get(opt_keys.prompt_description),
                            arguments=handler.opt.get(opt_keys.prompt_arguments),
                            icons=handler.opt.get(opt_keys.prompt_icons),
                            completions=handler.opt.get(opt_keys.prompt_completions),
                        )

            if getattr(handler, "route_handlers", None):
                self._discover_mcp_routes(handler.route_handlers)  # pyright: ignore[reportAttributeAccessIssue]
