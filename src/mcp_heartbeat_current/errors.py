"""Adapter-level failures, kept distinct from the core's.

The core refuses *documents*; this package refuses *exchanges*. Mixing the
two vocabularies would let a transport fault be reported as a lineage
violation, which is how an operator ends up chasing a clock-skew bug that
is really a misrouted gateway.

Every error here carries a JSON-RPC ``code`` from
:mod:`~mcp_heartbeat_current.contract`, so a transport can serialise it
without a translation table.
"""
from __future__ import annotations

from typing import Any

from .contract import (
    HEARTBEAT_EXTENSION_ID,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    UNSUPPORTED_PROTOCOL_VERSION,
)


class AdapterError(Exception):
    """Base class for every current-adapter failure."""

    code: int = INVALID_REQUEST

    def __init__(self, message: str, *, data: Any = None) -> None:
        self.data = data
        super().__init__(message)

    def to_error(self) -> dict[str, Any]:
        """Render as a JSON-RPC error object."""
        error: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.data is not None:
            error["data"] = self.data
        return error


class MalformedEnvelope(AdapterError):
    """A request omitted or malformed the modern ``_meta`` envelope."""

    code = INVALID_PARAMS


class HeaderMismatch(AdapterError):
    """A standard header disagreed with the body it is required to mirror."""

    code = HEADER_MISMATCH


class UnsupportedProtocolVersion(AdapterError):
    """The peer proposed a revision this adapter does not speak.

    ``data.supported`` names what *is* speakable, which is what lets a
    conformant peer retry once rather than give up on the server entirely.
    """

    code = UNSUPPORTED_PROTOCOL_VERSION

    def __init__(self, proposed: object, supported: tuple[str, ...]) -> None:
        self.proposed = proposed
        self.supported = supported
        super().__init__(
            f"unsupported protocol version {proposed!r} (supported: {', '.join(supported)})",
            data={"supported": list(supported)},
        )


class UnsupportedHeartbeatExtension(AdapterError):
    """The peer's heartbeat extension version is unreadable by this build.

    Deliberately *not* a protocol-level error: the MCP connection is fine
    and every other capability keeps working. Only heartbeat is disabled.
    """

    code = METHOD_NOT_FOUND

    def __init__(self, found: object, supported: str) -> None:
        self.found = found
        self.supported = supported
        super().__init__(
            f"unsupported heartbeat extension version {found!r} (supported: {supported})",
            data={"extension": HEARTBEAT_EXTENSION_ID, "supported": supported},
        )


class ForbiddenPrimitiveUsed(AdapterError):
    """A legacy primitive was seen on, or attempted from, the modern path."""

    code = INVALID_REQUEST


class CrossEraConfusion(AdapterError):
    """A message mixed legacy and modern mechanics in one exchange.

    The dangerous case is not a wrong version number — it is a message that
    is *plausible* under both eras, because that is what a gateway will
    forward and both sides will half-accept.
    """

    code = INVALID_REQUEST


class IdentityUnbound(AdapterError):
    """An authenticated principal may not publish the claimed participant.

    A security event, not a liveness event: something proved who it was and
    then claimed to be someone else. Fails closed.
    """

    code = INVALID_REQUEST


class SubscriptionProtocolError(AdapterError):
    """A ``subscriptions/listen`` stream broke its own ordering contract."""

    code = INVALID_REQUEST


__all__ = [
    "AdapterError",
    "CrossEraConfusion",
    "ForbiddenPrimitiveUsed",
    "HeaderMismatch",
    "IdentityUnbound",
    "MalformedEnvelope",
    "SubscriptionProtocolError",
    "UnsupportedHeartbeatExtension",
    "UnsupportedProtocolVersion",
]
