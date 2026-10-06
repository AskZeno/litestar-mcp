"""Opaque answers for exceptions no handler mapped.

An unmapped exception's text can carry internals (SQL, hostnames, ids), so it
is never what the caller reads. The exception is logged once at ERROR with a
fresh reference, and the caller reads only that reference, which leads an
operator to the record.
"""

from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    import logging

UNMAPPED_FAILURE = "The tool failed. Reference: {reference}"
"""What a caller reads for an unmapped exception."""

UNMAPPED_STATUS = 500
"""The status an unmapped exception answers with: a server failure, retryable."""


def log_unmapped(logger: "logging.Logger", exc: "BaseException", message: "str", *args: "Any") -> "str":
    """Log ``exc`` once at ERROR under a fresh reference and return the reference."""
    reference = uuid4().hex
    logger.error(
        "%s (reference=%s)",
        message % args,
        reference,
        exc_info=exc,
        extra={"mcp_error_reference": reference},
    )
    return reference


def unmapped_text(reference: "str") -> "str":
    """What a caller reads for an unmapped exception logged under ``reference``."""
    return UNMAPPED_FAILURE.format(reference=reference)


def unmapped_content(reference: "str") -> "list[dict[str, Any]]":
    """The content blocks for an unmapped exception logged under ``reference``."""
    return [{"type": "text", "text": unmapped_text(reference)}]
