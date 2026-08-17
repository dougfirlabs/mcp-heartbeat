"""Two version axes, negotiated separately and reported separately.

The MCP **protocol era** and the heartbeat **extension version** are
independent (`docs/heartbeat-0.1.md`; epic §"Independent version axes"), and
conflating them is how a peer ends up believing a 0.1 heartbeat implies a
particular revision of MCP. So they are negotiated by two functions, carried
in two fields of :class:`EraReport`, and never collapsed into one string.

This module is also where defect **D-04** is repaired. The archived legacy
server echoed whatever ``protocolVersion`` a client asserted — including
revisions that have never existed — and recorded nothing. Here every request
is checked against a closed supported set, every disagreement is a first-class
value the caller can log, and anything unsupported or ambiguous fails closed.

Stdlib plus the portable core. No transport, no SDK.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mcp_heartbeat.model import EXTENSION_VERSION

#: The revision this adapter is written against.
LEGACY_ERA = "2025-06-18"

#: Every protocol revision the legacy adapter will negotiate. Closed set: a
#: revision that is not listed here is refused, never echoed.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26")

#: The current era. Named here only so the legacy path can *refuse* it
#: explicitly — a legacy connection must not silently claim modern semantics.
MODERN_ERA = "2026-07-28"

#: Where a legacy peer advertises the heartbeat extension. ``experimental`` is
#: the legacy home for unregistered capabilities; the modern era replaces it
#: with a prefixed ``capabilities.extensions`` identifier (defect D-07, owned
#: by HB-03), which this adapter must never advertise.
HEARTBEAT_NAMESPACE = "experimental"
HEARTBEAT_CAPABILITY = "presenceLease"

#: The modern extension identifier. Present so its appearance on a legacy
#: session is detected as an ambiguous dual-era claim rather than ignored.
MODERN_EXTENSION_ID = "com.dougfirlabs/heartbeat"

#: Lifecycle and resource primitives that belong to the legacy era alone. The
#: modern adapter (HB-03) must not call any of them; ``assert_legacy_era``
#: is the runtime seam that makes crossing the boundary an error.
LEGACY_ONLY_PRIMITIVES = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "resources/subscribe",
        "resources/unsubscribe",
        "notifications/resources/updated",
    }
)


class NegotiationOutcome(str, Enum):
    """Three outcomes, because "not accepted" hides the interesting half.

    ``UNSUPPORTED`` means the peer named something real that we do not serve.
    ``AMBIGUOUS`` means we could not tell what the peer named at all. Both
    fail closed; only the second is a sign the peer is malformed rather than
    merely from a different era.
    """

    ACCEPTED = "accepted"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


class NegotiationCode(str, Enum):
    """Stable reason for a negotiation result."""

    SUPPORTED_VERSION = "supported_version"
    UNSUPPORTED_VERSION = "unsupported_version"
    MODERN_ERA_REFUSED = "modern_era_refused"
    VERSION_MISSING = "version_missing"
    VERSION_MALFORMED = "version_malformed"

    CAPABILITY_ACCEPTED = "capability_accepted"
    CAPABILITY_ABSENT = "capability_absent"
    CAPABILITY_MALFORMED = "capability_malformed"
    CAPABILITY_AMBIGUOUS = "capability_ambiguous"
    UNSUPPORTED_EXTENSION_VERSION = "unsupported_extension_version"


_MISSING = object()


@dataclass(frozen=True)
class ProtocolNegotiation:
    """The outcome of one ``initialize`` version exchange.

    ``negotiated`` is ``None`` unless the request was accepted, which is what
    makes an echo impossible: there is no value to send back.
    """

    outcome: NegotiationOutcome
    code: NegotiationCode
    requested: object
    negotiated: str | None
    detail: str

    @property
    def accepted(self) -> bool:
        return self.outcome is NegotiationOutcome.ACCEPTED

    @property
    def disagreement(self) -> bool:
        """True whenever the peer asked for something we did not accept.

        D-04's other half: the archived server had no notion of a
        disagreement, so a downgrade-confusion test could not be written
        against it. Every one of these is recorded in the session ledger.
        """
        return not self.accepted

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "code": self.code.value,
            "requested": self.requested if isinstance(self.requested, str) else repr(self.requested),
            "negotiated": self.negotiated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HeartbeatNegotiation:
    """The outcome of the heartbeat *capability* exchange.

    Deliberately not fatal on its own. A peer that says nothing about
    heartbeats is an ordinary MCP peer, and the session continues — it is
    simply reported heartbeat-unsupported.
    """

    outcome: NegotiationOutcome
    code: NegotiationCode
    extension_version: str | None
    detail: str

    @property
    def supported(self) -> bool:
        return self.outcome is NegotiationOutcome.ACCEPTED


@dataclass(frozen=True)
class EraReport:
    """What era this session speaks, and which heartbeat contract rides on it.

    The two fields are separately named and separately populated; a hard
    constraint of this PRD is that the adapter reports them apart. A session
    can be a perfectly good ``2025-06-18`` session with
    ``heartbeat_supported`` false, and that is not a degraded state.
    """

    mcp_protocol_era: str | None
    extension_version: str | None
    heartbeat_supported: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "mcp_protocol_era": self.mcp_protocol_era,
            "extension_version": self.extension_version,
            "heartbeat_supported": self.heartbeat_supported,
        }


UNNEGOTIATED = EraReport(
    mcp_protocol_era=None, extension_version=None, heartbeat_supported=False
)


def negotiate_protocol(requested: object = _MISSING) -> ProtocolNegotiation:
    """Check a requested ``protocolVersion`` against the supported set.

    Never echoes. An unknown revision is ``UNSUPPORTED``, a missing or
    non-string one is ``AMBIGUOUS``, and the modern era gets its own code so
    the log says *why* a 2026 client was turned away rather than filing it
    with typos.
    """
    if requested is _MISSING or requested is None:
        return ProtocolNegotiation(
            NegotiationOutcome.AMBIGUOUS,
            NegotiationCode.VERSION_MISSING,
            None,
            None,
            "initialize carried no protocolVersion; the legacy contract requires one",
        )
    if not isinstance(requested, str) or not requested:
        return ProtocolNegotiation(
            NegotiationOutcome.AMBIGUOUS,
            NegotiationCode.VERSION_MALFORMED,
            requested,
            None,
            "protocolVersion must be a non-empty string",
        )
    if requested == MODERN_ERA:
        return ProtocolNegotiation(
            NegotiationOutcome.UNSUPPORTED,
            NegotiationCode.MODERN_ERA_REFUSED,
            requested,
            None,
            f"{MODERN_ERA} is served by the current adapter, not the legacy one",
        )
    if requested not in SUPPORTED_PROTOCOL_VERSIONS:
        return ProtocolNegotiation(
            NegotiationOutcome.UNSUPPORTED,
            NegotiationCode.UNSUPPORTED_VERSION,
            requested,
            None,
            f"unsupported protocolVersion {requested!r}; "
            f"supported: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
        )
    return ProtocolNegotiation(
        NegotiationOutcome.ACCEPTED,
        NegotiationCode.SUPPORTED_VERSION,
        requested,
        requested,
        "negotiated the revision the peer requested",
    )


def negotiate_heartbeat(capabilities: object) -> HeartbeatNegotiation:
    """Decide whether this peer speaks the heartbeat extension.

    Absence is the ordinary case for an unknown peer and is reported as
    unsupported, not as an error. Ambiguity — an unreadable advertisement, or
    a peer claiming the legacy *and* modern identifiers at once — fails
    closed, because a session that cannot say which era it is in must not be
    allowed to pick one later.
    """
    if capabilities is None:
        capabilities = {}
    if not isinstance(capabilities, dict):
        return HeartbeatNegotiation(
            NegotiationOutcome.AMBIGUOUS,
            NegotiationCode.CAPABILITY_MALFORMED,
            None,
            "capabilities must be an object",
        )

    experimental = capabilities.get(HEARTBEAT_NAMESPACE) or {}
    legacy_claim = experimental.get(HEARTBEAT_CAPABILITY) if isinstance(experimental, dict) else None
    extensions = capabilities.get("extensions") or {}
    modern_claim = extensions.get(MODERN_EXTENSION_ID) if isinstance(extensions, dict) else None

    if legacy_claim is not None and modern_claim is not None:
        return HeartbeatNegotiation(
            NegotiationOutcome.AMBIGUOUS,
            NegotiationCode.CAPABILITY_AMBIGUOUS,
            None,
            "peer advertises both the legacy and the modern heartbeat identifier; "
            "a legacy session cannot also claim current MCP semantics",
        )
    if legacy_claim is None:
        if modern_claim is not None:
            return HeartbeatNegotiation(
                NegotiationOutcome.UNSUPPORTED,
                NegotiationCode.MODERN_ERA_REFUSED,
                None,
                f"{MODERN_EXTENSION_ID} is a modern-era identifier; "
                "the legacy adapter does not serve it",
            )
        return HeartbeatNegotiation(
            NegotiationOutcome.UNSUPPORTED,
            NegotiationCode.CAPABILITY_ABSENT,
            None,
            "peer advertises no heartbeat capability; ordinary MCP continues",
        )
    if not isinstance(legacy_claim, dict):
        return HeartbeatNegotiation(
            NegotiationOutcome.AMBIGUOUS,
            NegotiationCode.CAPABILITY_MALFORMED,
            None,
            f"{HEARTBEAT_NAMESPACE}.{HEARTBEAT_CAPABILITY} must be an object",
        )

    declared = legacy_claim.get("extension_version")
    if declared is None:
        return HeartbeatNegotiation(
            NegotiationOutcome.AMBIGUOUS,
            NegotiationCode.CAPABILITY_MALFORMED,
            None,
            "heartbeat capability declares no extension_version",
        )
    if declared != EXTENSION_VERSION:
        return HeartbeatNegotiation(
            NegotiationOutcome.UNSUPPORTED,
            NegotiationCode.UNSUPPORTED_EXTENSION_VERSION,
            None,
            f"extension_version {declared!r} is not readable by this build "
            f"(supported: {EXTENSION_VERSION})",
        )
    return HeartbeatNegotiation(
        NegotiationOutcome.ACCEPTED,
        NegotiationCode.CAPABILITY_ACCEPTED,
        EXTENSION_VERSION,
        "heartbeat extension negotiated on a legacy session",
    )


def assert_legacy_era(era: object) -> str:
    """Return ``era`` if the legacy adapter owns it; raise otherwise.

    The seam that keeps the two adapters apart. Called wherever an era value
    crosses into legacy code, so a modern revision reaching this path is an
    exception at the boundary rather than a session nobody owns.
    """
    from .errors import LegacyEraViolation

    if era == MODERN_ERA:
        raise LegacyEraViolation(
            f"{MODERN_ERA} belongs to the current adapter; "
            "no legacy lifecycle primitive is callable from the modern path"
        )
    if not isinstance(era, str) or era not in SUPPORTED_PROTOCOL_VERSIONS:
        raise LegacyEraViolation(
            f"{era!r} is not a protocol revision the legacy adapter serves "
            f"({', '.join(SUPPORTED_PROTOCOL_VERSIONS)})"
        )
    return era


__all__ = [
    "HEARTBEAT_CAPABILITY",
    "HEARTBEAT_NAMESPACE",
    "LEGACY_ERA",
    "LEGACY_ONLY_PRIMITIVES",
    "MODERN_ERA",
    "MODERN_EXTENSION_ID",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "UNNEGOTIATED",
    "EraReport",
    "HeartbeatNegotiation",
    "NegotiationCode",
    "NegotiationOutcome",
    "ProtocolNegotiation",
    "assert_legacy_era",
    "negotiate_heartbeat",
    "negotiate_protocol",
]
