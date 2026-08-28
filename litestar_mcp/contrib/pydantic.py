"""Pydantic v2 ownership for MCP tool models.

Importing this optional module fails with litestar-mcp's actionable
missing-dependency error when Pydantic is unavailable. The core package
does not otherwise require Pydantic.
"""

from typing import Annotated, Any, get_args, get_origin

from litestar_mcp.exceptions import MissingDependencyError
from litestar_mcp.validation import ValidationIssue

try:
    from pydantic import BaseModel, TypeAdapter, ValidationError
except ImportError as exc:  # pragma: no cover - exercised without the optional extra
    package = "pydantic"
    raise MissingDependencyError(package) from exc

__all__ = ("PydanticToolTypeAdapter",)


class PydanticToolTypeAdapter:
    """Validate and describe Pydantic ``BaseModel`` annotations."""

    def supports_type(self, annotation: "Any") -> "bool":
        annotation = _unwrap_annotated(annotation)
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)

    def validate(self, value: "Any", annotation: "Any") -> "list[ValidationIssue]":
        try:
            TypeAdapter(annotation).validate_python(value)
        except ValidationError as exc:
            return [
                ValidationIssue(
                    message=str(error["msg"]),
                    path=_location_path(error.get("loc", ())),
                )
                for error in exc.errors()
            ]
        return []

    def json_schema(self, annotation: "Any") -> "dict[str, Any] | None":
        schema = TypeAdapter(annotation).json_schema()
        return dict(schema)


def _unwrap_annotated(annotation: "Any") -> "Any":
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _location_path(location: "Any") -> "str":
    if not isinstance(location, (tuple, list)) or not location:
        return ""
    return "$" + "".join(f".{part}" for part in location)
