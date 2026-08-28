"""Pluggable type ownership for MCP tool validation and JSON Schema.

The first adapter whose :meth:`ToolTypeAdapter.supports_type` returns
``True`` owns an annotation. The msgspec adapter terminates every chain
and preserves the library's historical permissive behavior for types
msgspec does not know how to construct.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

import msgspec

__all__ = (
    "MsgspecToolTypeAdapter",
    "ToolTypeAdapter",
    "ValidationIssue",
    "resolve_type_adapters",
    "type_adapter_for",
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One adapter validation issue with a msgspec-style ``$`` path."""

    message: "str"
    path: "str" = ""


@runtime_checkable
class ToolTypeAdapter(Protocol):
    """Own validation and optional JSON Schema for one family of types."""

    def supports_type(self, annotation: "Any") -> "bool":
        """Return whether this adapter owns ``annotation``."""

    def validate(self, value: "Any", annotation: "Any") -> "list[ValidationIssue]":
        """Return validation issues; an empty list means accepted."""

    def json_schema(self, annotation: "Any") -> "dict[str, Any] | None":
        """Return JSON Schema when owned, or ``None`` for legacy fallback."""


class MsgspecToolTypeAdapter:
    """Terminal adapter preserving the existing ``msgspec.convert`` contract."""

    def supports_type(self, annotation: "Any") -> "bool":
        return True

    def validate(self, value: "Any", annotation: "Any") -> "list[ValidationIssue]":
        try:
            msgspec.convert(value, annotation, strict=False)
        except msgspec.ValidationError as exc:
            message, path = _split_msgspec_error(exc)
            return [ValidationIssue(message=message, path=path)]
        except TypeError:
            # Historical behavior: custom types msgspec cannot introspect are
            # left to Litestar's own request pipeline instead of rejected.
            return []
        return []

    def json_schema(self, annotation: "Any") -> "dict[str, Any] | None":
        try:
            if isinstance(annotation, type) and issubclass(annotation, msgspec.Struct):
                return msgspec.json.schema(annotation)
        except TypeError:
            pass
        return None


def type_adapter_for(annotation: "Any", adapters: "tuple[ToolTypeAdapter, ...]") -> "ToolTypeAdapter":
    """Select the first owner; a resolved chain always has a terminal adapter."""
    for adapter in adapters:
        if adapter.supports_type(annotation):
            return adapter
    msg = "Tool type adapter chain has no terminal owner"
    raise RuntimeError(msg)


def resolve_type_adapters(
    configured: "Sequence[ToolTypeAdapter] | None" = None,
    *,
    include_pydantic: "bool" = False,
) -> "tuple[ToolTypeAdapter, ...]":
    """Build a first-match chain and append the msgspec terminal adapter."""
    adapters: list[ToolTypeAdapter] = list(configured or ())
    if configured is None and include_pydantic:
        from litestar_mcp.contrib.pydantic import PydanticToolTypeAdapter

        adapters.append(PydanticToolTypeAdapter())
    if not any(isinstance(adapter, MsgspecToolTypeAdapter) for adapter in adapters):
        adapters.append(MsgspecToolTypeAdapter())
    return tuple(adapters)


def _split_msgspec_error(exc: "Exception") -> "tuple[str, str]":
    """Split ``msgspec.ValidationError`` text into reason and ``$`` path."""
    text = str(exc)
    marker = " - at `"
    if marker in text and text.endswith("`"):
        reason, _, tail = text.rpartition(marker)
        return reason, tail[:-1]
    return text, ""
