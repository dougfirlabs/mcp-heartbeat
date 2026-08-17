"""The pinned MCP 2026-07-28 contract, declared once.

Every literal the current adapter binds to lives here and nowhere else:
protocol revisions, ``_meta`` key names, standard request headers, JSON-RPC
error codes, and the heartbeat extension identifier. Two consequences that
are the point of the module:

* **The extension version and the protocol revision are separate axes.**
  ``HEARTBEAT_EXTENSION_VERSION`` comes from the portable core and versions
  *the lease contract*; ``PROTOCOL_REVISION`` versions *the MCP wire*. A
  test asserts neither is derived from the other, because conflating them
  is exactly the HB-00 defect this PRD exists to avoid repeating.
* **Every value is verifiable against the official SDK.** Each constant
  below was read out of ``mcp``/``mcp-types`` 2.0.0 at the revision named in
  :data:`SDK_PIN`, and :mod:`mcp_heartbeat_current.sdk` re-checks them
  against the installed SDK so drift fails a test rather than a deployment.

This module imports the standard library and the portable core only. The
official SDK is imported by :mod:`~mcp_heartbeat_current.sdk`, which is the
only module in the package that may.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Mapping

from mcp_heartbeat.model import EXTENSION_VERSION as HEARTBEAT_EXTENSION_VERSION

# ── protocol revisions ────────────────────────────────────────────────

#: The revision this adapter speaks.
PROTOCOL_REVISION: Final = "2026-07-28"

#: Revisions that convey version, identity and capabilities as per-request
#: metadata. Ordered oldest → newest, matching ``mcp_types.version``, so
#: ``mutual[-1]`` is the highest shared revision.
MODERN_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = ("2026-07-28",)

#: Revisions that open with an ``initialize`` handshake. Named so the
#: adapter can *recognise and refuse* them, never to speak them.
HANDSHAKE_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)

# ── the heartbeat extension ───────────────────────────────────────────

#: Implementation-owned extension identifier (operator decision D-N5).
#:
#: Deliberately *not* under ``io.modelcontextprotocol/`` — that namespace
#: belongs to the standards body, and claiming it would assert an
#: endorsement that does not exist. If a vendor-neutral identifier is ever
#: assigned, this constant is the only line that changes.
HEARTBEAT_EXTENSION_ID: Final = "com.dougfirlabs/heartbeat"

#: ``_meta`` key carrying revision/digest alongside a resource-changed
#: notification. Prefixed, per the modern ``_meta`` key naming rules.
HEARTBEAT_HINT_META_KEY: Final = HEARTBEAT_EXTENSION_ID

# ── standard per-request metadata and headers ─────────────────────────

PROTOCOL_VERSION_META_KEY: Final = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META_KEY: Final = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META_KEY: Final = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META_KEY: Final = "io.modelcontextprotocol/serverInfo"
SUBSCRIPTION_ID_META_KEY: Final = "io.modelcontextprotocol/subscriptionId"

MCP_PROTOCOL_VERSION_HEADER: Final = "mcp-protocol-version"
MCP_METHOD_HEADER: Final = "mcp-method"
MCP_NAME_HEADER: Final = "mcp-name"

#: Methods whose ``Mcp-Name`` header mirrors a named request parameter.
NAME_BEARING_METHODS: Final[Mapping[str, str]] = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}

# ── JSON-RPC error codes ──────────────────────────────────────────────

INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
HEADER_MISMATCH: Final = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY: Final = -32021
UNSUPPORTED_PROTOCOL_VERSION: Final = -32022

#: HTTP status a transport returns for each ladder rejection.
ERROR_CODE_HTTP_STATUS: Final[Mapping[int, int]] = {
    INVALID_PARAMS: 400,
    HEADER_MISMATCH: 400,
    UNSUPPORTED_PROTOCOL_VERSION: 400,
}

# ── methods this adapter uses ─────────────────────────────────────────

DISCOVER_METHOD: Final = "server/discover"
LISTEN_METHOD: Final = "subscriptions/listen"
READ_RESOURCE_METHOD: Final = "resources/read"
ACKNOWLEDGED_NOTIFICATION: Final = "notifications/subscriptions/acknowledged"
RESOURCE_UPDATED_NOTIFICATION: Final = "notifications/resources/updated"

#: Legacy lifecycle methods. Named here so :mod:`~mcp_heartbeat_current.era`
#: can *recognise and refuse* them; no other module may spell them.
HANDSHAKE_METHODS: Final[tuple[str, ...]] = ("initialize", "notifications/initialized")

#: Legacy headers a gateway may still inject. 2026-07-28 removed protocol
#: sessions and resumable streams, so these are dropped on receipt and never
#: echoed — recorded on the route rather than silently discarded.
IGNORED_LEGACY_HEADERS: Final[tuple[str, ...]] = ("mcp-session-id", "last-event-id")

#: Notification methods a ``subscriptions/listen`` stream may carry. The
#: server MUST NOT send a type the client did not ask for, so a receiver
#: that sees anything outside this set has been handed a protocol error.
LISTEN_STREAM_METHODS: Final[frozenset[str]] = frozenset(
    {
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
        "notifications/resources/list_changed",
        RESOURCE_UPDATED_NOTIFICATION,
    }
)


# ── forbidden primitives (HB-00 protocol-era-matrix, `current.forbidden`) ──


@dataclass(frozen=True)
class ForbiddenPrimitive:
    """A legacy primitive the current path may recognise but never emit."""

    primitive: str
    kind: str
    requirement_level: str
    rationale: str


#: Reproduced from ``docs/evidence/mcp-heartbeat-hb00/protocol-era-matrix.json``
#: so the lint has a machine-readable target that travels with the package.
FORBIDDEN_PRIMITIVES: Final[tuple[ForbiddenPrimitive, ...]] = (
    ForbiddenPrimitive(
        "initialize",
        "rpc",
        "MUST NOT (modern path)",
        "There is no negotiation handshake in 2026-07-28; discovery replaces it.",
    ),
    ForbiddenPrimitive(
        "notifications/initialized",
        "notification",
        "MUST NOT (modern path)",
        "Part of the legacy handshake only; modern is stateless per request.",
    ),
    ForbiddenPrimitive(
        "Mcp-Session-Id",
        "http_header",
        "MUST NOT mint or echo",
        "2026-07-28 removed protocol-level sessions; the header is ignored, never issued.",
    ),
    ForbiddenPrimitive(
        "GET <mcp endpoint> -> text/event-stream",
        "transport",
        "MUST NOT serve; SHOULD respond 405",
        "The standalone notification stream moved to the subscriptions/listen response.",
    ),
    ForbiddenPrimitive(
        "resources/subscribe",
        "rpc",
        "MUST NOT (modern path)",
        "Explicitly replaced by subscriptions/listen.",
    ),
    ForbiddenPrimitive(
        "Last-Event-ID",
        "http_header",
        "MUST NOT rely on",
        "Resumable SSE streams are not supported in this revision.",
    ),
    ForbiddenPrimitive(
        "server-initiated JSON-RPC requests on an SSE stream",
        "transport",
        "MUST NOT",
        "Server-to-client interaction is embedded in an InputRequiredResult instead.",
    ),
    ForbiddenPrimitive(
        "experimental.presenceLease",
        "capability",
        "MUST NOT (as a modern extension identifier)",
        "Modern extension identifiers require a mandatory prefix; this one has none.",
    ),
)


# ── official SDK pin ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SdkPin:
    """The exact official Python SDK revision this adapter was built against."""

    distribution: str
    version: str
    types_distribution: str
    types_version: str
    implements_revision: str
    requires_python: str

    def to_dict(self) -> dict[str, str]:
        return {
            "distribution": self.distribution,
            "version": self.version,
            "types_distribution": self.types_distribution,
            "types_version": self.types_version,
            "implements_revision": self.implements_revision,
            "requires_python": self.requires_python,
        }


#: Verified during HB-00 (``official_python_sdk.latest_version == "2.0.0"``)
#: and re-verified here by installing it. Deliberately an exact ``==`` pin:
#: a range would let a minor release change a wire constant silently.
SDK_PIN: Final = SdkPin(
    distribution="mcp",
    version="2.0.0",
    types_distribution="mcp-types",
    types_version="2.0.0",
    implements_revision=PROTOCOL_REVISION,
    requires_python=">=3.10",
)


# ── era predicates ────────────────────────────────────────────────────


def is_modern(version: str) -> bool:
    """True when ``version`` carries its envelope as per-request metadata."""
    return version in MODERN_PROTOCOL_VERSIONS


def is_handshake(version: str) -> bool:
    """True when ``version`` opens with an ``initialize`` exchange."""
    return version in HANDSHAKE_PROTOCOL_VERSIONS


def highest_mutual_version(offered: object) -> str | None:
    """The newest revision in both ``offered`` and :data:`MODERN_PROTOCOL_VERSIONS`.

    Returns ``None`` when there is no overlap, which is a downgrade refusal
    rather than an error: the peer is simply not a modern peer.
    """
    if not isinstance(offered, (list, tuple, set, frozenset)):
        return None
    mutual = [v for v in MODERN_PROTOCOL_VERSIONS if v in offered]
    return mutual[-1] if mutual else None


# ── extension identifier grammar ──────────────────────────────────────

_LABEL = r"[A-Za-z](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_IDENTIFIER_RE: Final = re.compile(rf"{_LABEL}(?:\.{_LABEL})*/{_NAME}")


def is_valid_extension_identifier(identifier: object) -> bool:
    """True when ``identifier`` satisfies the prefixed ``_meta`` key grammar.

    Mirrors ``mcp.shared.extension.validate_extension_identifier``. The copy
    exists so the pure layer can validate without the SDK installed;
    :func:`mcp_heartbeat_current.sdk.assert_contract_matches_sdk` proves the
    two agree on a shared corpus, so the copy cannot silently diverge.
    """
    return isinstance(identifier, str) and bool(_IDENTIFIER_RE.fullmatch(identifier))


__all__ = [
    "ACKNOWLEDGED_NOTIFICATION",
    "CLIENT_CAPABILITIES_META_KEY",
    "CLIENT_INFO_META_KEY",
    "DISCOVER_METHOD",
    "ERROR_CODE_HTTP_STATUS",
    "FORBIDDEN_PRIMITIVES",
    "ForbiddenPrimitive",
    "HANDSHAKE_METHODS",
    "HANDSHAKE_PROTOCOL_VERSIONS",
    "HEADER_MISMATCH",
    "HEARTBEAT_EXTENSION_ID",
    "HEARTBEAT_EXTENSION_VERSION",
    "HEARTBEAT_HINT_META_KEY",
    "IGNORED_LEGACY_HEADERS",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "LISTEN_METHOD",
    "LISTEN_STREAM_METHODS",
    "MCP_METHOD_HEADER",
    "MCP_NAME_HEADER",
    "MCP_PROTOCOL_VERSION_HEADER",
    "METHOD_NOT_FOUND",
    "MISSING_REQUIRED_CLIENT_CAPABILITY",
    "MODERN_PROTOCOL_VERSIONS",
    "NAME_BEARING_METHODS",
    "PROTOCOL_REVISION",
    "PROTOCOL_VERSION_META_KEY",
    "READ_RESOURCE_METHOD",
    "RESOURCE_UPDATED_NOTIFICATION",
    "SDK_PIN",
    "SERVER_INFO_META_KEY",
    "SUBSCRIPTION_ID_META_KEY",
    "SdkPin",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "highest_mutual_version",
    "is_handshake",
    "is_modern",
    "is_valid_extension_identifier",
]
