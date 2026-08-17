"""The one seam that imports the official MCP Python SDK.

Everything else in this package is standard library plus the portable core,
which is what lets the adapter's logic be tested — and shipped — in an
environment where ``mcp`` is not installed. This module is where the pinned
SDK (:data:`~mcp_heartbeat_current.contract.SDK_PIN`) actually gets used:
a real :class:`mcp.server.extension.Extension` carrying the heartbeat
identifier, a real ``server/discover`` answered by the SDK's own handler, a
real ``subscriptions/listen`` served by :class:`mcp.server.subscriptions.ListenHandler`,
and a real client reading the lease over ``resources/read``.

:func:`assert_contract_matches_sdk` is the load-bearing function. Every
constant in :mod:`~mcp_heartbeat_current.contract` was transcribed from this
SDK; that function re-derives each one from the installed package and fails
if any has drifted. Without it the pure layer would be a *copy* of the
contract, which decays silently. With it, the copy is a checked assertion.

Import is guarded: ``import mcp_heartbeat_current.sdk`` succeeds with no SDK
present and :data:`SDK_AVAILABLE` is ``False``. Every builder below raises a
clear :class:`SdkUnavailable` instead of an obscure ``ModuleNotFoundError``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from mcp_heartbeat.model import Heartbeat

from . import contract
from .contract import HEARTBEAT_EXTENSION_ID, SDK_PIN
from .convergence import FetchResult
from .discovery import HeartbeatCapability, Negotiation, negotiate
from .identity import Principal
from .subscriptions import SubscriptionFilter

try:  # pragma: no cover - exercised by the isolated SDK venv
    import mcp  # noqa: F401
    import mcp_types
    from mcp.client import Client
    from mcp.server.mcpserver import MCPServer
    from mcp.server.extension import Extension
    from mcp.server.subscriptions import InMemorySubscriptionBus
    from mcp.shared.extension import validate_extension_identifier
    from mcp.shared.inbound import (
        MCP_METHOD_HEADER,
        MCP_NAME_HEADER,
        MCP_PROTOCOL_VERSION_HEADER,
        NAME_BEARING_METHODS,
    )
    from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
    from mcp_types.jsonrpc import (
        HEADER_MISMATCH,
        INVALID_PARAMS,
        INVALID_REQUEST,
        METHOD_NOT_FOUND,
        MISSING_REQUIRED_CLIENT_CAPABILITY,
        UNSUPPORTED_PROTOCOL_VERSION,
    )
    from mcp_types.version import (
        HANDSHAKE_PROTOCOL_VERSIONS,
        LATEST_MODERN_VERSION,
        MODERN_PROTOCOL_VERSIONS,
    )

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - the SDK-absent path
    SDK_AVAILABLE = False


class SdkUnavailable(RuntimeError):
    """The official SDK is not importable in this environment."""

    def __init__(self) -> None:
        super().__init__(
            f"the current adapter's SDK binding requires "
            f"{SDK_PIN.distribution}=={SDK_PIN.version} and "
            f"{SDK_PIN.types_distribution}=={SDK_PIN.types_version}; "
            "install them (see tools/verify_sdk.sh)"
        )


def require_sdk() -> None:
    """Raise :class:`SdkUnavailable` unless the pinned SDK is importable."""
    if not SDK_AVAILABLE:
        raise SdkUnavailable()


# ── provenance ────────────────────────────────────────────────────────


def sdk_provenance() -> dict[str, Any]:
    """Installed SDK versions and the pin they are checked against.

    Emitted as the PRD's "Official SDK version/provenance" evidence. The
    distinction between ``pinned`` and ``installed`` is the whole point: an
    artifact that only recorded one of them could not show a drift.
    """
    require_sdk()
    import importlib.metadata as md

    installed: dict[str, str | None] = {}
    for dist in (SDK_PIN.distribution, SDK_PIN.types_distribution):
        try:
            installed[dist] = md.version(dist)
        except md.PackageNotFoundError:  # pragma: no cover - require_sdk covers it
            installed[dist] = None

    return {
        "artifact": "sdk-provenance",
        "pinned": SDK_PIN.to_dict(),
        "installed": installed,
        "matches_pin": installed.get(SDK_PIN.distribution) == SDK_PIN.version
        and installed.get(SDK_PIN.types_distribution) == SDK_PIN.types_version,
        "modern_protocol_versions": list(MODERN_PROTOCOL_VERSIONS),
        "latest_modern_version": LATEST_MODERN_VERSION,
        "handshake_protocol_versions": list(HANDSHAKE_PROTOCOL_VERSIONS),
        "extension_identifier": HEARTBEAT_EXTENSION_ID,
    }


#: Identifiers used to prove the local grammar copy agrees with the SDK's.
#: Includes the ones that matter: a bare name, a name with no prefix, and
#: the legacy capability key the modern revision forbids.
_IDENTIFIER_CORPUS: tuple[str, ...] = (
    HEARTBEAT_EXTENSION_ID,
    "io.modelcontextprotocol/tasks",
    "com.example/thing-1",
    "a.b.c/d.e-f",
    "nodot/name",
    "noslash",
    "",
    "/leading",
    "trailing/",
    "com.dougfirlabs/",
    "-bad.prefix/name",
    "com.dougfirlabs/heartbeat lease",
)


def assert_contract_matches_sdk() -> dict[str, Any]:
    """Re-derive every pinned constant from the installed SDK.

    Returns the comparison table on success so it can be archived as
    evidence; raises :class:`AssertionError` naming the first divergence.
    """
    require_sdk()

    checks: list[tuple[str, Any, Any]] = [
        ("MODERN_PROTOCOL_VERSIONS", contract.MODERN_PROTOCOL_VERSIONS, tuple(MODERN_PROTOCOL_VERSIONS)),
        ("PROTOCOL_REVISION", contract.PROTOCOL_REVISION, LATEST_MODERN_VERSION),
        (
            "HANDSHAKE_PROTOCOL_VERSIONS",
            contract.HANDSHAKE_PROTOCOL_VERSIONS,
            tuple(HANDSHAKE_PROTOCOL_VERSIONS),
        ),
        (
            "PROTOCOL_VERSION_META_KEY",
            contract.PROTOCOL_VERSION_META_KEY,
            mcp_types.PROTOCOL_VERSION_META_KEY,
        ),
        ("CLIENT_INFO_META_KEY", contract.CLIENT_INFO_META_KEY, mcp_types.CLIENT_INFO_META_KEY),
        (
            "CLIENT_CAPABILITIES_META_KEY",
            contract.CLIENT_CAPABILITIES_META_KEY,
            mcp_types.CLIENT_CAPABILITIES_META_KEY,
        ),
        ("SERVER_INFO_META_KEY", contract.SERVER_INFO_META_KEY, mcp_types.SERVER_INFO_META_KEY),
        (
            "SUBSCRIPTION_ID_META_KEY",
            contract.SUBSCRIPTION_ID_META_KEY,
            SUBSCRIPTION_ID_META_KEY,
        ),
        (
            "MCP_PROTOCOL_VERSION_HEADER",
            contract.MCP_PROTOCOL_VERSION_HEADER,
            MCP_PROTOCOL_VERSION_HEADER,
        ),
        ("MCP_METHOD_HEADER", contract.MCP_METHOD_HEADER, MCP_METHOD_HEADER),
        ("MCP_NAME_HEADER", contract.MCP_NAME_HEADER, MCP_NAME_HEADER),
        (
            "NAME_BEARING_METHODS",
            dict(contract.NAME_BEARING_METHODS),
            dict(NAME_BEARING_METHODS),
        ),
        ("HEADER_MISMATCH", contract.HEADER_MISMATCH, HEADER_MISMATCH),
        (
            "UNSUPPORTED_PROTOCOL_VERSION",
            contract.UNSUPPORTED_PROTOCOL_VERSION,
            UNSUPPORTED_PROTOCOL_VERSION,
        ),
        (
            "MISSING_REQUIRED_CLIENT_CAPABILITY",
            contract.MISSING_REQUIRED_CLIENT_CAPABILITY,
            MISSING_REQUIRED_CLIENT_CAPABILITY,
        ),
        ("INVALID_PARAMS", contract.INVALID_PARAMS, INVALID_PARAMS),
        ("INVALID_REQUEST", contract.INVALID_REQUEST, INVALID_REQUEST),
        ("METHOD_NOT_FOUND", contract.METHOD_NOT_FOUND, METHOD_NOT_FOUND),
    ]

    table: list[dict[str, Any]] = []
    for name, ours, theirs in checks:
        assert ours == theirs, f"contract.{name} = {ours!r} but the SDK says {theirs!r}"
        table.append({"constant": name, "value": _jsonable(theirs), "matches": True})

    # The identifier grammar is a behavioural copy, so compare behaviour.
    grammar: list[dict[str, Any]] = []
    for candidate in _IDENTIFIER_CORPUS:
        try:
            validate_extension_identifier(candidate, owner="heartbeat-conformance")
            sdk_ok = True
        except (TypeError, ValueError):
            sdk_ok = False
        ours_ok = contract.is_valid_extension_identifier(candidate)
        assert ours_ok == sdk_ok, (
            f"extension identifier {candidate!r}: local grammar says {ours_ok}, SDK says {sdk_ok}"
        )
        grammar.append({"identifier": candidate, "valid": sdk_ok})

    assert contract.PROTOCOL_REVISION in MODERN_PROTOCOL_VERSIONS, (
        f"{contract.PROTOCOL_REVISION} is not a modern revision in this SDK"
    )

    return {
        "artifact": "sdk-contract-conformance",
        "sdk": sdk_provenance(),
        "constants": table,
        "extension_identifier_grammar": grammar,
    }


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return list(value) if isinstance(value, (tuple, set, frozenset)) else str(value)


# ── server ────────────────────────────────────────────────────────────


@dataclass
class HeartbeatPublication:
    """The leases one server publishes, keyed by participant.

    Mutable on purpose: a test advances a participant's lease and then
    publishes a change event, which is exactly what a real producer does.
    """

    documents: dict[str, Mapping[str, Any]]

    def put(self, heartbeat: Heartbeat) -> None:
        self.documents[heartbeat.participant_id] = heartbeat.to_dict()

    def get(self, participant_id: str) -> Mapping[str, Any] | None:
        return self.documents.get(participant_id)


def build_heartbeat_extension(capability: HeartbeatCapability | None = None) -> "Extension":
    """A real SDK ``Extension`` advertising ``com.dougfirlabs/heartbeat``.

    Defined inside the function because :class:`mcp.server.extension.Extension`
    validates ``identifier`` in ``__init_subclass__`` — the class cannot even
    be *defined* with a non-conformant id, which is a nice property to rely
    on but means the subclass must not exist at import time in an SDK-less
    environment.
    """
    require_sdk()
    settings = (capability or HeartbeatCapability()).to_dict()

    class HeartbeatExtension(Extension):
        identifier = HEARTBEAT_EXTENSION_ID

        def settings(self) -> dict[str, Any]:
            return dict(settings)

    return HeartbeatExtension()


def build_heartbeat_server(
    publication: HeartbeatPublication,
    *,
    name: str = "mcp-heartbeat-current",
    capability: HeartbeatCapability | None = None,
) -> tuple["MCPServer", Any]:
    """An ``MCPServer`` serving leases as resources, with a listen bus.

    Returns ``(server, bus)``. The bus is handed back so a producer can
    publish a resource-updated event; a test that could only trigger changes
    through the server's own tools would not be able to model a *lost*
    notification, which is the case that matters most.
    """
    require_sdk()
    bus = InMemorySubscriptionBus()
    server = MCPServer(
        name,
        extensions=[build_heartbeat_extension(capability)],
        subscriptions=bus,
    )
    template = (capability or HeartbeatCapability()).resource_uri_template

    @server.resource(template, mime_type="application/json")
    def heartbeat(participant_id: str) -> str:
        decoded = HeartbeatCapability.participant_from_uri_segment(participant_id)
        document = publication.get(decoded)
        if document is None:
            raise ValueError(f"no heartbeat published for {decoded!r}")
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    return server, bus


# ── client ────────────────────────────────────────────────────────────


async def discover_raw(client: "Client", version: str | None = None) -> dict[str, Any]:
    """Send a real ``server/discover`` and return the unparsed result.

    Goes through :meth:`mcp.client.session.ClientSession.send_discover`, so
    what comes back is the server's actual wire result rather than a
    client-side reconstruction — which is what makes it usable as the PRD's
    "current protocol transcript" evidence.
    """
    require_sdk()
    return await client.session.send_discover(version or LATEST_MODERN_VERSION)


async def discover(client: "Client", version: str | None = None) -> Negotiation:
    """Run real modern discovery and read it into a :class:`Negotiation`.

    Note what is absent: no ``initialize``, no session id, no stream. One
    request, and the client knows the revision, the capabilities and whether
    heartbeat is available.
    """
    return negotiate(await discover_raw(client, version))


async def read_heartbeat(
    client: "Client", negotiation: Negotiation, participant_id: str
) -> Mapping[str, Any]:
    """Fetch the authoritative lease over ``resources/read``.

    ``cache_mode="bypass"`` because a heartbeat is the one resource for
    which a warm cache entry is exactly the wrong answer — the question
    being asked is "is this current *right now*".
    """
    require_sdk()
    if negotiation.capability is None:
        raise SdkUnavailable()
    uri = negotiation.capability.resource_uri(participant_id)
    result = await client.read_resource(uri, cache_mode="bypass")
    text = result.contents[0].text  # type: ignore[union-attr]
    return json.loads(text)


async def fetch_result(
    client: "Client",
    negotiation: Negotiation,
    participant_id: str,
    principal: Principal | None = None,
) -> FetchResult:
    """A :class:`FetchResult` for :class:`~.convergence.HeartbeatConsumer`.

    ``principal`` is passed in by the caller rather than read from ambient
    state: on the client side there is no authenticated context to read, and
    inventing one would reintroduce the channel-level binding D-05 was.
    """
    document = await read_heartbeat(client, negotiation, participant_id)
    return FetchResult(document=document, principal=principal)


def listen_filter(negotiation: Negotiation, participant_ids: Sequence[str]) -> SubscriptionFilter:
    """The ``subscriptions/listen`` filter for a set of participants."""
    if negotiation.capability is None:
        return SubscriptionFilter()
    return SubscriptionFilter.for_participants(
        negotiation.capability.resource_uri(p) for p in participant_ids
    )


__all__ = [
    "SDK_AVAILABLE",
    "HeartbeatPublication",
    "SdkUnavailable",
    "assert_contract_matches_sdk",
    "build_heartbeat_extension",
    "build_heartbeat_server",
    "discover",
    "fetch_result",
    "listen_filter",
    "read_heartbeat",
    "require_sdk",
    "sdk_provenance",
]
