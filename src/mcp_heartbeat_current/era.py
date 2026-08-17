"""Explicit era routing — and refusing anything that could be either.

One endpoint may serve both MCP eras. That is useful and it is also the
most dangerous thing in this package, because the failure mode is not a
rejected request: it is a request that *both* sides half-accept. A legacy
client whose ``initialize`` is answered by a modern server thinks it holds
a negotiated session; a modern envelope smuggled through a legacy path
gets its ``_meta`` ignored and its version silently downgraded.

So routing here is a classification with exactly three outcomes, and the
third one is loud:

* :attr:`Era.CURRENT` — a modern per-request envelope is present.
* :attr:`Era.HANDSHAKE` — a legacy lifecycle method, with no modern envelope.
* a raised :class:`~mcp_heartbeat_current.errors.CrossEraConfusion` — the
  message carries mechanics from both eras, or from neither.

This module is one of only three permitted to spell a forbidden primitive
(see :mod:`~mcp_heartbeat_current.lint`), and it spells them only to refuse
them. Nothing here constructs a legacy message, and no legacy code path is
renamed or reused: HB-03 is greenfield construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .contract import (
    HANDSHAKE_METHODS,
    HANDSHAKE_PROTOCOL_VERSIONS,
    IGNORED_LEGACY_HEADERS,
    MCP_PROTOCOL_VERSION_HEADER,
    MODERN_PROTOCOL_VERSIONS,
    PROTOCOL_VERSION_META_KEY,
    is_modern,
)
from .errors import CrossEraConfusion, ForbiddenPrimitiveUsed
from .metadata import ModernRoute, classify_request

#: Legacy RPCs a modern peer must never call. ``resources/subscribe`` is
#: here rather than in ``HANDSHAKE_METHODS`` because it is not lifecycle —
#: it is the change-delivery primitive that ``subscriptions/listen``
#: replaced, and confusing the two is how a "modern" client ends up with no
#: notifications at all.
LEGACY_ONLY_METHODS: tuple[str, ...] = (*HANDSHAKE_METHODS, "resources/subscribe")


class Era(str, Enum):
    """Which MCP era a message belongs to."""

    CURRENT = "current"
    HANDSHAKE = "handshake"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class EraRoute:
    """A message, classified, with the evidence for the classification.

    ``evidence`` exists so era selection is *observable*: an operator
    reading a log can see that a request routed modern because it carried
    a protocol-version ``_meta`` key, not because a heuristic guessed.
    """

    era: Era
    method: str
    protocol_version: str | None
    evidence: tuple[str, ...]
    modern: ModernRoute | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "era": self.era.value,
            "method": self.method,
            "protocol_version": self.protocol_version,
            "evidence": list(self.evidence),
            "ignored_headers": list(self.modern.ignored_headers) if self.modern else [],
        }


def has_modern_envelope(body: Mapping[str, Any]) -> bool:
    """True when the body declares a protocol version in ``_meta``.

    This one key is the era discriminator in 2026-07-28, matching the
    official SDK's own detector. Deliberately not "does it look modern":
    a single, checkable presence test is what makes the classification
    reproducible.
    """
    params = body.get("params")
    if not isinstance(params, Mapping):
        return False
    meta = params.get("_meta")
    return isinstance(meta, Mapping) and PROTOCOL_VERSION_META_KEY in meta


def is_handshake_method(body: Mapping[str, Any]) -> bool:
    """True when the body is a legacy lifecycle message."""
    return body.get("method") in HANDSHAKE_METHODS


def route(
    body: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> EraRoute:
    """Classify one inbound message, or refuse it as cross-era.

    Both-eras and neither-era are refused for the same reason: a message
    that a gateway can plausibly forward to either backend is one that will
    eventually be forwarded to the wrong one.
    """
    method = body.get("method")
    if not isinstance(method, str) or not method:
        raise CrossEraConfusion("message carries no JSON-RPC method, so no era can be determined")

    modern_envelope = has_modern_envelope(body)
    handshake_method = is_handshake_method(body)

    if modern_envelope and handshake_method:
        raise CrossEraConfusion(
            f"{method!r} is a legacy lifecycle method carrying a modern _meta envelope"
        )

    if modern_envelope:
        if method in LEGACY_ONLY_METHODS:
            raise CrossEraConfusion(f"{method!r} does not exist on the modern path")
        modern = classify_request(body, headers)
        return EraRoute(
            era=Era.CURRENT,
            method=method,
            protocol_version=modern.protocol_version,
            evidence=(f"_meta[{PROTOCOL_VERSION_META_KEY}]={modern.protocol_version}",),
            modern=modern,
        )

    if handshake_method:
        proposed = _proposed_handshake_version(body)
        if proposed is not None and is_modern(proposed):
            raise CrossEraConfusion(
                f"{method!r} proposes modern revision {proposed!r}; "
                "modern revisions have no handshake"
            )
        return EraRoute(
            era=Era.HANDSHAKE,
            method=method,
            protocol_version=proposed,
            evidence=(f"method={method}", f"params.protocolVersion={proposed}"),
        )

    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    header_version = lowered.get(MCP_PROTOCOL_VERSION_HEADER)
    if header_version is not None:
        raise CrossEraConfusion(
            f"{method!r} carries the {MCP_PROTOCOL_VERSION_HEADER} header "
            "but no modern _meta envelope"
        )
    raise CrossEraConfusion(
        f"{method!r} carries neither a modern _meta envelope nor a legacy handshake"
    )


def _proposed_handshake_version(body: Mapping[str, Any]) -> str | None:
    params = body.get("params")
    if not isinstance(params, Mapping):
        return None
    version = params.get("protocolVersion")
    return version if isinstance(version, str) else None


def refuse_on_current_path(body: Mapping[str, Any]) -> None:
    """Raise when ``body`` uses a primitive forbidden on the modern path."""
    method = body.get("method")
    if method in LEGACY_ONLY_METHODS:
        raise ForbiddenPrimitiveUsed(
            f"{method!r} is forbidden on the MCP {MODERN_PROTOCOL_VERSIONS[-1]} path"
        )


def downgrade_refused(offered_versions: object) -> bool:
    """True when a peer offers only handshake-era revisions.

    Not an error — it is a correct, expected outcome when a modern-only
    consumer meets a legacy-only server. The caller reports "no mutual era"
    and stops, rather than falling back and pretending the eras are
    interchangeable.
    """
    if not isinstance(offered_versions, (list, tuple, set, frozenset)):
        return True
    offered = set(offered_versions)
    return not (offered & set(MODERN_PROTOCOL_VERSIONS)) and bool(
        offered & set(HANDSHAKE_PROTOCOL_VERSIONS)
    )


def leaked_legacy_headers(headers: Mapping[str, str] | None) -> tuple[str, ...]:
    """Legacy transport headers present on a modern exchange.

    Present-and-ignored is the conformant behaviour; *reporting* them is
    what turns a silently tolerated gateway misconfiguration into something
    an operator can see.
    """
    lowered = {str(k).lower() for k in (headers or {})}
    return tuple(h for h in IGNORED_LEGACY_HEADERS if h in lowered)


__all__ = [
    "LEGACY_ONLY_METHODS",
    "Era",
    "EraRoute",
    "downgrade_refused",
    "has_modern_envelope",
    "is_handshake_method",
    "leaked_legacy_headers",
    "refuse_on_current_path",
    "route",
]
