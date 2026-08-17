"""``server/discover`` and heartbeat extension negotiation.

Modern discovery is one request that returns supported revisions,
capabilities and identity together. There is no handshake, so a server that
answers ``server/discover`` has already told a client everything it needs —
including whether it speaks heartbeat, under the prefixed identifier
:data:`~mcp_heartbeat_current.contract.HEARTBEAT_EXTENSION_ID`.

The negotiation below keeps two axes strictly apart:

* the **protocol revision**, chosen as the highest mutual modern revision;
* the **heartbeat extension version**, read out of the extension's own
  settings block.

Either can fail without the other. A peer speaking 2026-07-28 with a
heartbeat extension from the future is a *usable MCP peer with heartbeat
disabled* — that distinction is the whole of acceptance criterion "unsupported
versions fail clearly without disabling unrelated MCP features", and it is
why :class:`Negotiation` carries the untouched capability map alongside its
verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote, unquote

from .contract import (
    DISCOVER_METHOD,
    HEARTBEAT_EXTENSION_ID,
    HEARTBEAT_EXTENSION_VERSION,
    LISTEN_METHOD,
    MODERN_PROTOCOL_VERSIONS,
    PROTOCOL_REVISION,
    READ_RESOURCE_METHOD,
    highest_mutual_version,
    is_valid_extension_identifier,
)
from .errors import UnsupportedHeartbeatExtension, UnsupportedProtocolVersion

#: Where a participant's authoritative lease is read from. A template, not a
#: fixed URI: one server publishes many participants, and the consumer must
#: be able to address each without a side channel.
#:
#: The id sits in the *path*, and is percent-encoded on the way in. A
#: participant id may legally contain ``/``, ``:`` and ``+``
#: (``docs/heartbeat-0.1.md`` §3), all of which are URI-significant — an
#: unencoded ``svc/api-7`` silently becomes two path segments and stops
#: matching the template.
DEFAULT_RESOURCE_URI_TEMPLATE = "heartbeat://participants/{participant_id}"

#: Upper bound this adapter advertises on a lease window, in seconds.
#: Advertised so a consumer can size its refetch schedule from discovery
#: alone, before it has ever fetched a lease.
DEFAULT_MAX_LEASE_SECONDS = 300.0


@dataclass(frozen=True)
class HeartbeatCapability:
    """What the server publishes under ``capabilities.extensions[<id>]``.

    Everything here is *about the transport binding*. The lease document
    itself is the portable core's business and is not restated, so this
    block can never drift into a second, competing schema.
    """

    extension_version: str = HEARTBEAT_EXTENSION_VERSION
    resource_uri_template: str = DEFAULT_RESOURCE_URI_TEMPLATE
    max_lease_seconds: float = DEFAULT_MAX_LEASE_SECONDS
    #: True when the server serves ``subscriptions/listen``. Change delivery
    #: is an optimisation, so a server may honestly advertise ``False`` and
    #: still be fully conformant — consumers fall back to timed refetch.
    change_hints: bool = True
    #: True when the server reports per-response identity binding.
    identity_binding: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_version": self.extension_version,
            "resource_uri_template": self.resource_uri_template,
            "max_lease_seconds": self.max_lease_seconds,
            "change_hints": self.change_hints,
            "identity_binding": self.identity_binding,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HeartbeatCapability":
        return cls(
            extension_version=str(raw.get("extension_version", "")),
            resource_uri_template=str(
                raw.get("resource_uri_template", DEFAULT_RESOURCE_URI_TEMPLATE)
            ),
            max_lease_seconds=float(raw.get("max_lease_seconds", DEFAULT_MAX_LEASE_SECONDS)),
            change_hints=bool(raw.get("change_hints", False)),
            identity_binding=bool(raw.get("identity_binding", False)),
        )

    def resource_uri(self, participant_id: str) -> str:
        """The lease URI for ``participant_id``, percent-encoded."""
        return self.resource_uri_template.format(
            participant_id=quote(participant_id, safe="")
        )

    @staticmethod
    def participant_from_uri_segment(segment: str) -> str:
        """Reverse :meth:`resource_uri`'s encoding of one participant id."""
        return unquote(segment)


def build_discover_result(
    *,
    server_info: Mapping[str, Any],
    capability: HeartbeatCapability | None = None,
    other_capabilities: Mapping[str, Any] | None = None,
    instructions: str | None = None,
    serves_listen: bool = True,
) -> dict[str, Any]:
    """The ``server/discover`` result a heartbeat-serving peer returns.

    ``resources.subscribe`` and the ``listChanged`` flags derive from whether
    ``subscriptions/listen`` is actually served, not from a hand-set option.
    A capability that claims a stream the server will not open is worse than
    no capability at all: the consumer waits instead of polling.
    """
    if not is_valid_extension_identifier(HEARTBEAT_EXTENSION_ID):  # pragma: no cover - constant
        raise ValueError(f"{HEARTBEAT_EXTENSION_ID!r} is not a conformant extension identifier")

    capability = capability if capability is not None else HeartbeatCapability()
    capabilities: dict[str, Any] = {
        "resources": {"subscribe": serves_listen, "listChanged": serves_listen},
        "extensions": {
            **dict(other_capabilities.get("extensions", {}) if other_capabilities else {}),
            HEARTBEAT_EXTENSION_ID: capability.to_dict(),
        },
    }
    for key, value in (other_capabilities or {}).items():
        if key not in ("extensions", "resources"):
            capabilities[key] = value

    return {
        "supportedVersions": list(MODERN_PROTOCOL_VERSIONS),
        "capabilities": capabilities,
        "serverInfo": dict(server_info),
        "instructions": instructions,
        "resultType": "complete",
    }


def build_discover_request(
    *,
    request_id: str | int,
    protocol_version: str = PROTOCOL_REVISION,
    client_info: Mapping[str, Any] | None = None,
    client_capabilities: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The single request that replaces the legacy handshake."""
    from .metadata import build_request

    return build_request(
        DISCOVER_METHOD,
        {},
        request_id=request_id,
        protocol_version=protocol_version,
        client_info=client_info or {},
        client_capabilities=client_capabilities or {},
    )


@dataclass(frozen=True)
class Negotiation:
    """The outcome of discovery, on both axes.

    ``heartbeat_enabled`` is False whenever heartbeat is unusable *for any
    reason*, while ``protocol_revision`` stays populated — so a caller can
    always tell "this peer is unreachable" from "this peer is fine, I just
    cannot do heartbeat with it".
    """

    protocol_revision: str
    supported_versions: tuple[str, ...]
    heartbeat_enabled: bool
    extension_version: str | None = None
    capability: HeartbeatCapability | None = None
    #: Every non-heartbeat capability, untouched. Proof that refusing the
    #: extension did not disable anything else.
    other_capabilities: Mapping[str, Any] = field(default_factory=dict)
    #: Why heartbeat is off, when it is. ``None`` means it is on.
    disabled_reason: str | None = None

    @property
    def resource_uri_template(self) -> str | None:
        return self.capability.resource_uri_template if self.capability else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_revision": self.protocol_revision,
            "supported_versions": list(self.supported_versions),
            "heartbeat_enabled": self.heartbeat_enabled,
            "extension_version": self.extension_version,
            "extension_id": HEARTBEAT_EXTENSION_ID if self.heartbeat_enabled else None,
            "disabled_reason": self.disabled_reason,
            "other_capabilities": sorted(self.other_capabilities),
        }


def negotiate(
    discover_result: Mapping[str, Any],
    *,
    supported_extension_version: str = HEARTBEAT_EXTENSION_VERSION,
    strict_extension: bool = False,
) -> Negotiation:
    """Read a ``server/discover`` result into a :class:`Negotiation`.

    Raises :class:`~mcp_heartbeat_current.errors.UnsupportedProtocolVersion`
    when there is no mutual modern revision — that one *is* fatal, because
    without a shared revision there is no MCP conversation to have.

    An unusable heartbeat extension is reported, not raised, unless
    ``strict_extension``. The default is deliberate: a consumer that treats
    "no heartbeat" as "no server" will take a working deployment offline
    over an optional capability.
    """
    revision = highest_mutual_version(discover_result.get("supportedVersions"))
    if revision is None:
        raise UnsupportedProtocolVersion(
            discover_result.get("supportedVersions"), MODERN_PROTOCOL_VERSIONS
        )

    capabilities = discover_result.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    extensions = capabilities.get("extensions")
    extensions = extensions if isinstance(extensions, Mapping) else {}
    others = {k: v for k, v in capabilities.items() if k != "extensions"}
    others["extensions"] = {k: v for k, v in extensions.items() if k != HEARTBEAT_EXTENSION_ID}

    def refuse(reason: str, *, version: str | None = None) -> Negotiation:
        if strict_extension:
            raise UnsupportedHeartbeatExtension(version, supported_extension_version)
        return Negotiation(
            protocol_revision=revision,
            supported_versions=MODERN_PROTOCOL_VERSIONS,
            heartbeat_enabled=False,
            extension_version=version,
            other_capabilities=others,
            disabled_reason=reason,
        )

    raw = extensions.get(HEARTBEAT_EXTENSION_ID)
    if not isinstance(raw, Mapping):
        return refuse("extension_absent")

    capability = HeartbeatCapability.from_dict(raw)
    if capability.extension_version != supported_extension_version:
        return refuse("unsupported_extension_version", version=capability.extension_version)

    return Negotiation(
        protocol_revision=revision,
        supported_versions=MODERN_PROTOCOL_VERSIONS,
        heartbeat_enabled=True,
        extension_version=capability.extension_version,
        capability=capability,
        other_capabilities=others,
    )


def build_read_request(
    participant_id: str,
    negotiation: Negotiation,
    *,
    request_id: str | int,
    client_info: Mapping[str, Any] | None = None,
    client_capabilities: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The authoritative lease fetch for ``participant_id``.

    ``resources/read`` is name-bearing, so the returned headers carry
    ``Mcp-Name`` mirroring the URI — a conformance requirement that is easy
    to miss precisely because the request works without it against a lenient
    server, and then fails behind a strict gateway.
    """
    if not negotiation.heartbeat_enabled or negotiation.capability is None:
        raise UnsupportedHeartbeatExtension(
            negotiation.extension_version, HEARTBEAT_EXTENSION_VERSION
        )
    from .metadata import build_request

    uri = negotiation.capability.resource_uri(participant_id)
    return build_request(
        READ_RESOURCE_METHOD,
        {"uri": uri},
        request_id=request_id,
        protocol_version=negotiation.protocol_revision,
        client_info=client_info or {},
        client_capabilities=client_capabilities or {},
    )


def serves_change_hints(negotiation: Negotiation) -> bool:
    """Whether this peer will deliver hints over ``subscriptions/listen``."""
    return bool(
        negotiation.heartbeat_enabled
        and negotiation.capability is not None
        and negotiation.capability.change_hints
    )


__all__ = [
    "DEFAULT_MAX_LEASE_SECONDS",
    "DEFAULT_RESOURCE_URI_TEMPLATE",
    "DISCOVER_METHOD",
    "LISTEN_METHOD",
    "HeartbeatCapability",
    "Negotiation",
    "build_discover_request",
    "build_discover_result",
    "build_read_request",
    "negotiate",
    "serves_change_hints",
]
