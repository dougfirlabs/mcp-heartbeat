"""The declared client/server era pairs, each with an explicit verdict.

The PRD names six cases and requires that *every* one of them ends in
PASS, FAIL, or UNSUPPORTED — never in silence. This module is that
matrix, driven against the real adapters rather than against a
description of them: the legacy leg runs
:class:`~mcp_heartbeat_legacy.session.LegacyServerSession` against
:class:`~mcp_heartbeat_legacy.session.LegacyClientSession`, and the
current leg runs the current adapter's discovery, negotiation, and
convergence surface.

The dual-era server is modelled here as :class:`DualEraServer`: one
endpoint that classifies each request with the current adapter's own
``route()`` and then hands it to whichever leg owns it. That is the
topology the hard constraints care about, because it is the one a
gateway can confuse.

**Where UNSUPPORTED comes from, and why it is not a hedge.** The
official SDK v2 is an optional dependency of this package. When it is
absent, and no attestation covers this tree, the SDK-backed leg is
reported UNSUPPORTED with the pin it would have used — not PASS, because
nothing was proven, and not FAIL, because nothing was broken. The
release report then refuses to call the run publish-ready while that leg
is unproven. That chain is the point: a missing dependency degrades the
*verdict*, never the honesty.

The third state is an attested one. ``tools/verify_sdk.sh`` runs the leg
in a venv of its own — the SDK cannot be installed alongside a host application
without repinning ``pydantic-core`` underneath it — and writes what it
saw to ``docs/sdk-verification.json``. This module reads that record and
clears the case only while it still describes the tree in front of it;
see :mod:`.sdk_attestation` for the clauses. An absent, stale, or failing
record leaves the case exactly where it was.
"""
from __future__ import annotations

from typing import Any

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.issuer import HeartbeatIssuer
from mcp_heartbeat_current import contract, sdk
from mcp_heartbeat_current.convergence import Convergence, FetchResult, HeartbeatConsumer
from mcp_heartbeat_current.discovery import (
    HeartbeatCapability,
    build_discover_request,
    build_discover_result,
    build_read_request,
    negotiate,
)
from mcp_heartbeat_current.era import Era, downgrade_refused, route
from mcp_heartbeat_current.errors import (
    CrossEraConfusion,
    ForbiddenPrimitiveUsed,
    UnsupportedProtocolVersion,
)
from mcp_heartbeat_current.metadata import build_request
from mcp_heartbeat_legacy.capabilities import (
    AUTHORITATIVE_READ_METHOD,
    LIST_CHANGED_METHOD,
    SUBSCRIBE_METHOD,
    UNSUBSCRIBE_METHOD,
)
from mcp_heartbeat_legacy.era import LEGACY_ERA
from mcp_heartbeat_legacy.session import LegacyClientSession, LegacyServerSession

from . import sdk_attestation
from .verdicts import Case, MatrixReport, run_cases

PARTICIPANT = "svc/api-7"

#: What a legacy server must actually implement before it may advertise
#: the heartbeat extension. Declaring the capability without serving the
#: authoritative read is exactly the disagreement HB-02 closed, so the
#: conformance server here implements the real set.
LEGACY_IMPLEMENTED: tuple[str, ...] = (
    AUTHORITATIVE_READ_METHOD,
    SUBSCRIBE_METHOD,
    UNSUBSCRIBE_METHOD,
    LIST_CHANGED_METHOD,
)


def legacy_initialize(version: str = LEGACY_ERA) -> dict[str, Any]:
    """A handshake body as a legacy client on the wire would send it.

    Written here rather than taken from the legacy package so the
    negative cases prove the *router* refuses a confusable message,
    rather than proving the legacy package disagrees with itself.
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {"experimental": {"presenceLease": {"version": "0.1"}}},
            "clientInfo": {"name": "legacy-conformance", "version": "0.1.0"},
        },
    }


class DualEraServer:
    """One endpoint that serves both eras without ever mixing them.

    State is the whole risk here, so there is exactly one piece of it —
    the legacy session — and the modern path never touches it. The
    ``mutations`` ledger records every time a request reached something
    that could move heartbeat state, which is what lets a case assert
    that a refusal happened *first*.
    """

    def __init__(self) -> None:
        self.legacy = LegacyServerSession(
            server_name="dual-era-conformance", implemented=LEGACY_IMPLEMENTED
        )
        self.clock = FakeClock()
        self.issuer = HeartbeatIssuer(
            participant_id=PARTICIPANT, epoch_id="epoch-a", clock=self.clock
        )
        self.current = self.issuer.issue()
        #: Every request that reached a stateful handler, in order.
        self.mutations: list[str] = []
        #: Every request that was refused before dispatch, with its reason.
        self.refusals: list[dict[str, str]] = []

    def serve(self, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        """Classify then dispatch. Raises on any cross-era refusal."""
        try:
            routed = route(body, headers)
        except (CrossEraConfusion, UnsupportedProtocolVersion, ForbiddenPrimitiveUsed) as exc:
            self.refusals.append({"method": str(body.get("method")), "error": type(exc).__name__})
            raise

        method = str(body.get("method"))
        if routed.era is Era.CURRENT:
            self.mutations.append(f"current:{method}")
            return self._serve_current(method, body)
        self.mutations.append(f"legacy:{method}")
        return self.legacy.handle(method, body.get("params"))

    def _serve_current(self, method: str, body: dict[str, Any]) -> Any:
        if method == contract.DISCOVER_METHOD:
            return build_discover_result(
                server_info={"name": "dual-era-conformance", "version": "0.1.0"},
                capability=HeartbeatCapability(),
            )
        if method == contract.READ_RESOURCE_METHOD:
            return {"document": self.current.to_dict()}
        raise KeyError(f"unhandled modern method {method!r}")

    def beat(self) -> None:
        self.clock.advance(1.0)
        self.current = self.issuer.issue()


# ── the declared pairs ────────────────────────────────────────────────


def legacy_to_legacy(case: Case) -> None:
    """A legacy client and a legacy server complete a handshake and a lease."""
    server = LegacyServerSession(
        server_name="legacy-conformance", implemented=LEGACY_IMPLEMENTED
    )
    client = LegacyClientSession(client_name="legacy-conformance-client")

    method, params = client.initialize_request()
    result = server.handle(method, params)
    client.consume_initialize_result(result)
    server.handle(*client.initialized_notification())

    case.check("handshake_completed", server.heartbeat_ready and client.heartbeat_ready)
    case.check(
        "both_sides_agree_on_the_era",
        server.era_report.mcp_protocol_era == client.era_report.mcp_protocol_era,
        {
            "server": server.era_report.mcp_protocol_era,
            "client": client.era_report.mcp_protocol_era,
        },
    )
    # The two version axes must never be conflated into one string: the
    # protocol era is 2025-06-18 and the extension is 0.1, independently.
    case.check(
        "the_two_version_axes_stay_separate",
        server.era_report.mcp_protocol_era == LEGACY_ERA
        and server.era_report.extension_version == "0.1",
        server.era_report,
    )
    case.check("heartbeat_extension_was_negotiated", server.era_report.heartbeat_supported)
    case.observations = {"era_report": server.era_report}


def current_to_current(case: Case) -> None:
    """A current client and a current server discover, read, and converge."""
    clock = FakeClock()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="epoch-a", clock=clock)
    documents = {PARTICIPANT: issuer.issue().to_dict()}

    discover_body, discover_headers = build_discover_request(request_id=1)
    routed = route(discover_body, discover_headers)
    case.check("discovery_is_classified_current", routed.era is Era.CURRENT, routed.era)

    result = build_discover_result(
        server_info={"name": "current-conformance", "version": "0.1.0"},
        capability=HeartbeatCapability(),
    )
    negotiated = negotiate(result)
    case.check(
        "negotiated_the_modern_revision",
        negotiated.protocol_revision == contract.PROTOCOL_REVISION,
        negotiated.protocol_revision,
    )
    case.check(
        "negotiated_the_owned_extension_id",
        negotiated.to_dict()["extension_id"] == contract.HEARTBEAT_EXTENSION_ID,
        negotiated.to_dict()["extension_id"],
    )
    case.check(
        "the_extension_version_is_reported_separately_from_the_revision",
        negotiated.extension_version == "0.1",
        negotiated.extension_version,
    )

    read_body, _ = build_read_request(PARTICIPANT, negotiated, request_id=2)
    case.check("read_is_classified_current", route(read_body).era is Era.CURRENT)

    class Source:
        def fetch(self, participant_id: str) -> FetchResult:
            return FetchResult(document=documents[participant_id])

    consumer = HeartbeatConsumer(PARTICIPANT, Source(), clock)
    case.check("first_read_converges", consumer.refetch().convergence is Convergence.ADVANCED)
    clock.advance(1.0)
    documents[PARTICIPANT] = issuer.issue().to_dict()
    case.check("second_read_advances", consumer.refetch().convergence is Convergence.ADVANCED)
    case.check("no_handshake_was_needed", "initialize" not in str(read_body))
    case.observations = {"negotiation": negotiated, "held": consumer.held.revision if consumer.held else None}


def _attested_sdk_leg(case: Case) -> None:
    """Settle the SDK leg from a recorded run, or leave it UNSUPPORTED.

    The digest is computed here, over the tree as it stands, and compared
    against the one the run recorded. Nothing about the record is taken
    on trust: if it does not describe this tree the case is reported
    exactly as it was before any record existed.
    """
    digest = sdk_attestation.adapter_digest()
    record = sdk_attestation.load_attestation()

    if not sdk_attestation.attestation_covers(record, digest):
        case.observations = {
            "pin": contract.SDK_PIN.to_dict(),
            "adapter_digest": digest,
            "attestation": "stale or absent" if record else "absent",
        }
        case.unsupported(
            "the pinned official SDK v2 is not installed in this environment "
            "and no current attestation covers this tree; run "
            "tools/verify_sdk.sh to exercise this leg"
        )
        return

    assert record is not None  # attestation_covers is falsey on None
    case.check(
        "an_isolated_run_of_this_tree_was_recorded",
        record["adapter_digest"] == digest,
        {"attested_trees": record.get("attested_trees")},
    )
    case.check(
        "the_recorded_run_used_the_pinned_sdk",
        record["sdk_version"] == contract.SDK_PIN.version
        and record["sdk_types_version"] == contract.SDK_PIN.types_version,
        {record["sdk_distribution"]: record["sdk_version"],
         record["sdk_types_distribution"]: record["sdk_types_version"]},
    )
    case.check(
        "the_recorded_sdk_implements_the_pinned_revision",
        record["implements_revision"] == contract.PROTOCOL_REVISION,
        record["implements_revision"],
    )
    case.check(
        "the_recorded_run_passed_without_failures",
        record["tests_failed"] == 0 and record["tests_passed"] > 0,
        {"passed": record["tests_passed"], "failed": record["tests_failed"]},
    )
    case.observations = {"attestation": record}


def current_to_current_over_official_sdk(case: Case) -> None:
    """The same leg, but through the pinned official SDK v2.

    Three outcomes, in order of directness. With the SDK importable the
    contract is re-derived from it here and now. Without it, an
    attestation from ``tools/verify_sdk.sh`` stands in — but only while
    it covers this exact tree, so what clears the case is still a run
    that happened, not a claim that one did. Failing both, UNSUPPORTED.

    Kept separate from :func:`current_to_current` so an absent SDK
    degrades exactly one verdict instead of quietly weakening the
    adapter-level one.
    """
    if not sdk.SDK_AVAILABLE:
        _attested_sdk_leg(case)
        return

    provenance = sdk.assert_contract_matches_sdk()
    case.check("contract_matches_the_installed_sdk", bool(provenance), provenance)
    case.check(
        "the_installed_sdk_implements_the_pinned_revision",
        provenance.get("implements_revision") == contract.PROTOCOL_REVISION,
        provenance.get("implements_revision"),
    )
    case.observations = {"provenance": provenance}


def legacy_client_to_dual_era_server(case: Case) -> None:
    """A legacy client reaches the handshake leg of a dual-era endpoint."""
    server = DualEraServer()
    client = LegacyClientSession(client_name="legacy-into-dual")

    method, params = client.initialize_request()
    result = server.serve(dict(legacy_initialize(), method=method, params=params))
    client.consume_initialize_result(result)
    notify_method, notify_params = client.initialized_notification()
    server.serve({"jsonrpc": "2.0", "method": notify_method, "params": notify_params})

    case.check("handshake_leg_answered", client.heartbeat_ready)
    case.check(
        "served_on_the_legacy_path",
        all(entry.startswith("legacy:") for entry in server.mutations),
        server.mutations,
    )
    case.check("nothing_was_refused", server.refusals == [], server.refusals)
    case.observations = {"dispatch": server.mutations}


def current_client_to_dual_era_server(case: Case) -> None:
    """A current client reaches the modern leg of the same endpoint."""
    server = DualEraServer()

    discover_body, discover_headers = build_discover_request(request_id=1)
    result = server.serve(discover_body, discover_headers)
    negotiated = negotiate(result)
    read_body, read_headers = build_read_request(PARTICIPANT, negotiated, request_id=2)
    read = server.serve(read_body, read_headers)

    case.check("discovery_answered_on_the_modern_leg", "capabilities" in result, sorted(result))
    case.check("authoritative_read_answered", "document" in read, sorted(read))
    case.check(
        "served_on_the_current_path",
        all(entry.startswith("current:") for entry in server.mutations),
        server.mutations,
    )
    case.check("the_legacy_session_was_never_touched", not server.legacy.heartbeat_ready)
    case.check("nothing_was_refused", server.refusals == [], server.refusals)
    case.observations = {"dispatch": server.mutations}


def unsupported_version(case: Case) -> None:
    """Every revision outside the modern set is refused, not downgraded to."""
    server = DualEraServer()
    refused: dict[str, str] = {}

    for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2030-01-01"):
        body, headers = build_request("resources/read", {"uri": "heartbeat://a"}, request_id=1)
        body["params"]["_meta"][contract.PROTOCOL_VERSION_META_KEY] = version
        headers[contract.MCP_PROTOCOL_VERSION_HEADER] = version
        try:
            server.serve(body, headers)
            refused[version] = "ADMITTED"
        except (UnsupportedProtocolVersion, CrossEraConfusion) as exc:
            refused[version] = type(exc).__name__

    case.check(
        "every_unsupported_revision_was_refused",
        all(value != "ADMITTED" for value in refused.values()),
        refused,
    )
    case.check(
        "no_refused_request_reached_a_handler",
        server.mutations == [],
        server.mutations,
    )
    case.check(
        "a_legacy_only_offer_is_refused_rather_than_downgraded_to",
        downgrade_refused(["2025-06-18", "2025-11-25"]),
    )
    case.check(
        "a_mixed_offer_is_not_a_downgrade",
        not downgrade_refused(["2025-06-18", contract.PROTOCOL_REVISION]),
    )
    case.observations = {"per_version": refused}


def downgrade_confusion(case: Case) -> None:
    """Messages plausible to both eras are refused before dispatch.

    Each of these would be forwarded by a naive gateway to whichever
    backend answered first. The dual-era endpoint has to refuse all of
    them *and* still be able to serve honest traffic afterwards — a
    server that wedges on a confusing message has traded one failure for
    another.
    """
    server = DualEraServer()
    outcomes: dict[str, str] = {}

    confusable: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    handshake_with_envelope = legacy_initialize()
    handshake_with_envelope["params"]["_meta"] = {
        contract.PROTOCOL_VERSION_META_KEY: contract.PROTOCOL_REVISION
    }
    confusable.append(("handshake_carrying_a_modern_envelope", handshake_with_envelope, None))
    confusable.append(
        ("handshake_proposing_a_modern_revision", legacy_initialize(contract.PROTOCOL_REVISION), None)
    )

    subscribe_body, subscribe_headers = build_request(
        "resources/subscribe", {"uri": "heartbeat://svc/api-7"}, request_id=2
    )
    confusable.append(("modern_envelope_on_a_legacy_only_method", subscribe_body, subscribe_headers))
    confusable.append(
        (
            "modern_header_without_a_modern_body",
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "x"}},
            {contract.MCP_PROTOCOL_VERSION_HEADER: contract.PROTOCOL_REVISION},
        )
    )
    confusable.append(
        (
            "a_message_belonging_to_neither_era",
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "x"}},
            None,
        )
    )

    for label, body, headers in confusable:
        try:
            server.serve(body, headers)
            outcomes[label] = "ADMITTED"
        except (CrossEraConfusion, UnsupportedProtocolVersion, ForbiddenPrimitiveUsed) as exc:
            outcomes[label] = type(exc).__name__

    case.check(
        "every_confusable_message_was_refused",
        all(value != "ADMITTED" for value in outcomes.values()),
        outcomes,
    )
    case.check("no_confusable_message_reached_a_handler", server.mutations == [], server.mutations)
    case.check("all_five_were_attempted", len(outcomes) == 5, sorted(outcomes))

    # The endpoint still works afterwards.
    discover_body, discover_headers = build_discover_request(request_id=9)
    server.serve(discover_body, discover_headers)
    case.check("honest_traffic_still_served_afterwards", server.mutations == ["current:server/discover"])
    case.observations = {"per_message": outcomes, "refusals": server.refusals}


CASES: tuple[tuple[str, str, Any], ...] = (
    ("legacy-legacy", "A legacy client and a legacy server", legacy_to_legacy),
    ("current-current", "A current client and a current server (adapter surface)", current_to_current),
    ("current-current-sdk", "The same leg through the pinned official SDK v2", current_to_current_over_official_sdk),
    ("legacy-to-dual", "A legacy client against a dual-era server", legacy_client_to_dual_era_server),
    ("current-to-dual", "A current client against a dual-era server", current_client_to_dual_era_server),
    ("unsupported-version", "Unsupported revisions are refused, never downgraded to", unsupported_version),
    ("downgrade-confusion", "Messages plausible to both eras are refused by both", downgrade_confusion),
)


def run() -> MatrixReport:
    """Run the cross-era matrix. Always completes."""
    report = MatrixReport(
        matrix_id="cross-era",
        title="Declared client/server era pairs and their confusable neighbours",
    )
    return run_cases(report, CASES)


__all__ = ["CASES", "DualEraServer", "PARTICIPANT", "legacy_initialize", "run"]
