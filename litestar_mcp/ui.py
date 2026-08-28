"""MCP Apps extension vocabulary (SEP-1865, ``io.modelcontextprotocol/ui``).

Server-side contract only: capability settings, ``ui://`` declaration
conventions, tool↔UI linkage metadata, and startup validation. Host/View
runtime concerns (sandbox proxy, the ``ui/*`` postMessage dialect) are a
host application's job and out of this library's scope.
"""

from typing import Any

__all__ = (
    "UI_EXTENSION",
    "UI_MIME_TYPE",
    "UI_URI_SCHEME",
    "client_ui_mime_types",
    "normalized_ui_visibility",
)

UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME_TYPE = "text/html;profile=mcp-app"
UI_URI_SCHEME = "ui://"

_VISIBILITY_VALUES = ("model", "app")


def client_ui_mime_types(client_capabilities: "dict[str, Any] | None") -> "tuple[str, ...]":
    """The mimeTypes a client declared for the apps extension, or empty.

    SEP-1865 requires the ``mimeTypes`` settings field; a client declaring
    the extension without it (or with a non-list value) is treated as
    ui-incapable rather than invalid, matching graceful degradation.
    """
    extensions = (client_capabilities or {}).get("extensions")
    if not isinstance(extensions, dict):
        return ()
    settings = extensions.get(UI_EXTENSION)
    if not isinstance(settings, dict):
        return ()
    mime_types = settings.get("mimeTypes")
    if not isinstance(mime_types, list):
        return ()
    return tuple(value for value in mime_types if isinstance(value, str) and value.strip())


def normalized_ui_visibility(value: "Any") -> "tuple[str, ...] | None":
    """Validate a declared tool visibility list; ``None`` when absent."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        msg = "ui visibility must be a non-empty list drawn from ('model', 'app')"
        raise ValueError(msg)
    normalized = tuple(dict.fromkeys(str(entry) for entry in value))
    invalid = [entry for entry in normalized if entry not in _VISIBILITY_VALUES]
    if invalid:
        msg = f"ui visibility must be drawn from ('model', 'app'); got {invalid!r}"
        raise ValueError(msg)
    return normalized
