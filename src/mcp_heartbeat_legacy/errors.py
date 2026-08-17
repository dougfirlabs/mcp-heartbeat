"""Failures the legacy adapter raises, with stable machine-readable reasons.

Kept separate from :mod:`mcp_heartbeat.errors` on purpose. Those codes name
the *document* being wrong; these name the *conversation* being wrong, and the two
belong to different layers — a policy engine that keys on ``schema_invalid``
should not have to know that an MCP session exists.

Stdlib only, like the core.
"""
from __future__ import annotations

from enum import Enum


class JsonRpcCode(int, Enum):
    """The subset of JSON-RPC 2.0 codes the legacy contract actually uses."""

    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602


class LegacyReason(str, Enum):
    """Why the adapter refused. Stable strings; policy engines key on these."""

    #: An ordinary request arrived before the handshake completed (D-03).
    SESSION_NOT_INITIALIZED = "session_not_initialized"
    #: ``initialize`` arrived twice, or ``initialized`` did.
    LIFECYCLE_OUT_OF_ORDER = "lifecycle_out_of_order"
    #: Version negotiation did not produce a mutually supported era (D-04).
    NEGOTIATION_FAILED = "negotiation_failed"
    #: The peer answered with a revision we never offered (D-04).
    SILENT_DOWNGRADE_REFUSED = "silent_downgrade_refused"
    #: A method the server does not implement — including one it must
    #: therefore never have advertised (D-02).
    METHOD_NOT_IMPLEMENTED = "method_not_implemented"
    #: A change hint was structurally unusable (D-10).
    MALFORMED_HINT = "malformed_hint"
    #: Heartbeat work attempted on a session that never negotiated it.
    HEARTBEAT_NOT_NEGOTIATED = "heartbeat_not_negotiated"


class LegacyAdapterError(Exception):
    """Base class for every legacy-adapter failure."""

    reason: LegacyReason = LegacyReason.NEGOTIATION_FAILED


class LegacyProtocolError(LegacyAdapterError):
    """The legacy conversation cannot continue. Always fails closed.

    Carries a JSON-RPC code so a transport can render it as an error response
    verbatim, and a :class:`LegacyReason` so callers do not parse prose.
    """

    def __init__(
        self,
        reason: LegacyReason,
        message: str,
        *,
        code: JsonRpcCode = JsonRpcCode.INVALID_REQUEST,
    ) -> None:
        self.reason = reason
        self.code = code
        super().__init__(message)

    def to_dict(self) -> dict[str, object]:
        """Render as a JSON-RPC error object."""
        return {
            "code": int(self.code),
            "message": str(self),
            "data": {"reason": self.reason.value},
        }


class LegacyEraViolation(LegacyAdapterError):
    """A modern-era primitive reached the legacy path, or the reverse.

    The epic's boundary table forbids either direction. This exists so the
    violation is an exception at the seam rather than a silent dual-era
    session that neither adapter fully owns.
    """

    reason = LegacyReason.NEGOTIATION_FAILED


__all__ = [
    "JsonRpcCode",
    "LegacyAdapterError",
    "LegacyEraViolation",
    "LegacyProtocolError",
    "LegacyReason",
]
