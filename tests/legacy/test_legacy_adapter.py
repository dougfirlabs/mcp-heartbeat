"""Behaviour of the legacy adapter, end to end.

The corpus file pins *values*; this file pins *behaviour* — a real client and
a real server exchanging a real heartbeat over the legacy contract, and the
several ways that exchange must fail closed.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from mcp_heartbeat import FakeClock, HeartbeatIssuer
from mcp_heartbeat.model import EXTENSION_VERSION, IdentityBinding
from mcp_heartbeat_legacy import (
    EXTENDED_UPDATED_METHOD,
    LEGACY_ERA,
    MODERN_ERA,
    STANDARD_UPDATED_METHOD,
    SUBSCRIBE_METHOD,
    LegacyClientSession,
    LegacyEraViolation,
    LegacyHeartbeatConsumer,
    LegacyProtocolError,
    LegacyRefusal,
    LegacyServerSession,
    LegacySessionAuthContext,
    LegacySessionIdentityBinder,
    advertise,
    heartbeat_uri,
    parse_updated,
    updated_notifications,
)

PARTICIPANT = "acme/worker-1"
PRINCIPAL = "spiffe://acme/ns/prod/sa/worker"


class DictSource:
    """A :class:`~mcp_heartbeat.ports.HeartbeatSource` over a dict."""

    def __init__(self) -> None:
        self.documents: dict[str, Mapping[str, Any]] = {}

    def publish(self, document: Mapping[str, Any]) -> None:
        self.documents[document["node_id"]] = dict(document)

    def fetch(self, participant_id: str) -> Mapping[str, Any]:
        return self.documents[participant_id]


def connect(
    *,
    request_heartbeat: bool = True,
    implemented: tuple[str, ...] = ("resources/read", "resources/subscribe"),
) -> tuple[LegacyServerSession, LegacyClientSession]:
    """Run one complete legacy handshake and return both halves."""
    server = LegacyServerSession(
        server_name="lab",
        methods={name: (lambda params: {"served": True}) for name in implemented},
    )
    client = LegacyClientSession(client_name="probe", request_heartbeat=request_heartbeat)

    method, params = client.initialize_request()
    result = server.handle(method, params)
    client.consume_initialize_result(result)
    server.handle(*client.initialized_notification())
    return server, client


def issue(clock: FakeClock, source: DictSource, issuer: HeartbeatIssuer) -> Any:
    heartbeat = issuer.issue()
    source.publish(heartbeat.to_dict())
    return heartbeat


# ── story S1 · a known client and server exchange a heartbeat ─────────


def test_a_known_legacy_pair_exchanges_a_valid_minimal_heartbeat() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    server, client = connect()

    assert server.heartbeat_ready and client.heartbeat_ready

    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT, source=source, session=client
    )
    issue(clock, source, issuer)
    outcome = consumer.refetch(clock.now())

    assert outcome.accepted
    assert consumer.held is not None
    assert consumer.held.sequence == 0
    assert consumer.held.extension_version == EXTENSION_VERSION


def test_the_two_version_axes_are_reported_separately() -> None:
    server, client = connect()

    for report in (server.era_report, client.era_report):
        assert report.mcp_protocol_era == LEGACY_ERA
        assert report.extension_version == EXTENSION_VERSION
        assert report.heartbeat_supported is True
        # Distinct keys, distinct values, never one conflated string.
        assert set(report.to_dict()) == {
            "mcp_protocol_era",
            "extension_version",
            "heartbeat_supported",
        }


def test_an_unknown_peer_keeps_ordinary_mcp_and_is_heartbeat_unsupported() -> None:
    server, client = connect(request_heartbeat=False)

    # Ordinary MCP is entirely unaffected...
    assert server.state.value == "ready"
    assert server.handle("resources/read", {"uri": "file:///x"}) == {"served": True}
    # ...and the peer is reported heartbeat-unsupported rather than refused.
    assert server.era_report.mcp_protocol_era == LEGACY_ERA
    assert server.era_report.heartbeat_supported is False
    assert server.heartbeat_ready is False


def test_heartbeat_negotiation_is_bilateral() -> None:
    # A client that offered nothing does not acquire the extension because
    # the server advertises it...
    _, silent_client = connect(request_heartbeat=False)
    assert silent_client.heartbeat_ready is False

    # ...and a server that cannot serve the authoritative read does not
    # acquire it because the client asked. Either end may decline, and
    # declining is an ordinary MCP session, not an error.
    server, client = connect(implemented=("resources/list",))
    assert server.heartbeat_ready is False
    assert client.heartbeat_ready is False
    assert server.era_report.mcp_protocol_era == LEGACY_ERA
    assert server.state.value == "ready"


def test_heartbeat_work_on_an_unnegotiated_session_fails_closed() -> None:
    clock = FakeClock()
    source = DictSource()
    _, client = connect(request_heartbeat=False)
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT, source=source, session=client
    )

    outcome = consumer.refetch(clock.now())

    assert not outcome.accepted
    assert outcome.refused is LegacyRefusal.HEARTBEAT_NOT_NEGOTIATED
    assert consumer.held is None


# ── story S1 · era boundaries ─────────────────────────────────────────


def test_a_legacy_connection_cannot_claim_current_mcp_semantics() -> None:
    server = LegacyServerSession(server_name="lab", implemented={"resources/read"})

    with pytest.raises(LegacyProtocolError) as caught:
        server.handle("initialize", {"protocolVersion": MODERN_ERA, "capabilities": {}})

    assert caught.value.reason.value == "negotiation_failed"
    assert server.state.value == "failed"
    assert server.era_report.mcp_protocol_era is None


def test_no_legacy_primitive_is_reachable_with_a_modern_era() -> None:
    with pytest.raises(LegacyEraViolation):
        LegacyServerSession(server_name="lab", era=MODERN_ERA)
    with pytest.raises(LegacyEraViolation):
        LegacyClientSession(client_name="probe", requested_protocol_version=MODERN_ERA)


def test_a_client_refuses_a_revision_it_did_not_offer() -> None:
    client = LegacyClientSession(client_name="probe")
    client.initialize_request()

    with pytest.raises(LegacyProtocolError) as caught:
        client.consume_initialize_result(
            {"protocolVersion": "2025-03-26", "capabilities": {}}
        )

    assert caught.value.reason.value == "silent_downgrade_refused"
    assert client.state.value == "failed"


# ── story S1 · D-02, agreement between advertisement and registry ─────


def test_subscribe_is_advertised_exactly_when_it_is_served() -> None:
    server, _ = connect(implemented=("resources/read", SUBSCRIBE_METHOD))
    assert server.capabilities()["resources"]["subscribe"] is True

    bare, _ = connect(implemented=("resources/read",))
    assert bare.capabilities()["resources"]["subscribe"] is False

    with pytest.raises(LegacyProtocolError) as caught:
        bare.handle(SUBSCRIBE_METHOD, {"uri": heartbeat_uri(PARTICIPANT)})
    assert caught.value.reason.value == "method_not_implemented"


def test_the_advertisement_cannot_be_written_by_hand() -> None:
    # `advertise` takes only the registry, so there is no argument that could
    # claim a capability the registry does not back. That is the D-02 repair:
    # the two facts became one.
    derived = advertise({"resources/read"})
    assert derived["resources"] == {"subscribe": False, "listChanged": False}
    assert derived["experimental"]["presenceLease"]["extension_version"] == EXTENSION_VERSION


# ── story S1 · D-10, hints are advisory and refetch is authoritative ──


def test_a_standard_uri_only_hint_still_converges() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT, source=source, session=client
    )

    heartbeat = issue(clock, source, issuer)
    method, params = updated_notifications(heartbeat, extended=False)[0]
    assert method == STANDARD_UPDATED_METHOD
    assert params == {"uri": heartbeat_uri(PARTICIPANT)}

    outcome = consumer.refetch(clock.now(), hint=parse_updated(method, params))

    # Converged on the authoritative document, and the fact that the hint
    # could not corroborate it is counted rather than silently ignored.
    assert outcome.accepted
    assert outcome.hint_corroborated is None
    assert consumer.uncorroborated_hints == 1


def test_redelivery_of_the_same_revision_is_a_duplicate_not_a_failure() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT, source=source, session=client
    )

    issue(clock, source, issuer)
    assert consumer.refetch(clock.now()).accepted

    # A poller refetching an unchanged lease sees this constantly; it must not
    # read as a silent failure.
    again = consumer.refetch(clock.now())
    assert again.accepted is False
    assert again.duplicate is True
    assert again.reason is None


def test_the_extension_notification_carries_metadata_the_standard_one_cannot() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT, source=source, session=client
    )

    heartbeat = issue(clock, source, issuer)
    emitted = updated_notifications(heartbeat, extended=True)
    assert [method for method, _ in emitted] == [
        STANDARD_UPDATED_METHOD,
        EXTENDED_UPDATED_METHOD,
    ]
    # The standard notification stayed standard.
    assert set(emitted[0][1]) == {"uri"}

    outcome = consumer.refetch(clock.now(), hint=parse_updated(*emitted[1]))

    assert outcome.accepted
    assert outcome.hint_corroborated is True
    assert consumer.uncorroborated_hints == 0


def test_a_forged_hint_cannot_install_a_lease() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT, source=source, session=client
    )

    real = issue(clock, source, issuer)
    forged = parse_updated(
        EXTENDED_UPDATED_METHOD,
        {
            "uri": heartbeat_uri(PARTICIPANT),
            "revision": "e9:999",
            "digest": "sha256:" + "ff" * 32,
        },
    )

    outcome = consumer.refetch(clock.now(), hint=forged)

    # The held lease is the refetched one, not the one the hint described,
    # and the disagreement is reported rather than swallowed.
    assert outcome.accepted
    assert outcome.hint_corroborated is False
    assert consumer.held is not None
    assert consumer.held.revision == real.revision


# ── story S1 · identity binding on the legacy session ─────────────────


def binder(principal: str | None, permitted: dict[str, set[str]]) -> LegacySessionIdentityBinder:
    context = LegacySessionAuthContext(
        principal=principal, source="session_token" if principal else "none"
    )
    return LegacySessionIdentityBinder(context=context, permitted=permitted)


def test_a_permitted_principal_binds_and_the_evidence_names_the_context() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT,
        source=source,
        session=client,
        binder=binder(PRINCIPAL, {PRINCIPAL: {PARTICIPANT}}),
    )

    issue(clock, source, issuer)
    outcome = consumer.refetch(clock.now())

    assert outcome.accepted
    assert outcome.identity.binding is IdentityBinding.BOUND
    assert outcome.identity.principal == PRINCIPAL
    assert outcome.identity.context_source == "session_token"


def test_an_unbound_claim_fails_closed_and_keeps_the_previous_lease() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT,
        source=source,
        session=client,
        # The gateway authenticated, but for a different participant. This is
        # exactly the D-05 shape: one authenticated channel, many claims.
        binder=binder(PRINCIPAL, {PRINCIPAL: {"acme/worker-2"}}),
    )

    issue(clock, source, issuer)
    outcome = consumer.refetch(clock.now())

    assert not outcome.accepted
    assert outcome.refused is LegacyRefusal.IDENTITY_UNBOUND
    assert outcome.identity.binding is IdentityBinding.UNBOUND
    assert consumer.held is None


def test_publisher_identity_is_never_folded_into_content_verification() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT,
        source=source,
        session=client,
        binder=binder(PRINCIPAL, {PRINCIPAL: set()}),
    )

    issue(clock, source, issuer)
    outcome = consumer.refetch(clock.now())

    # The document is a perfectly valid next revision — the core says so —
    # and the publisher is still not permitted to have published it. Two
    # separate fields, two separate answers, and the adapter-level refusal is
    # what closes the door.
    assert outcome.admission.accepted is True
    assert outcome.admission.reason is None
    assert outcome.identity.binding is IdentityBinding.UNBOUND
    assert outcome.accepted is False
    assert outcome.reason == "identity_unbound"


def test_a_session_with_no_principal_is_unverified_not_bound() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT,
        source=source,
        session=client,
        binder=binder(None, {}),
    )

    issue(clock, source, issuer)
    outcome = consumer.refetch(clock.now())

    # Unverified is not a refusal — the claim is unchecked, not disproved —
    # but it is also never a promotion.
    assert outcome.accepted
    assert outcome.identity.binding is IdentityBinding.UNVERIFIED
    assert outcome.identity.principal is None


def test_no_binder_at_all_is_explicitly_unverified() -> None:
    clock = FakeClock()
    source = DictSource()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id="e1", clock=clock)
    _, client = connect()
    consumer = LegacyHeartbeatConsumer(
        participant_id=PARTICIPANT, source=source, session=client
    )

    issue(clock, source, issuer)
    outcome = consumer.refetch(clock.now())

    assert outcome.identity.binding is IdentityBinding.UNVERIFIED
    assert "no identity binder was injected" in outcome.identity.detail


def test_the_binding_facet_has_exactly_three_states() -> None:
    assert {member.value for member in IdentityBinding} == {
        "bound",
        "unbound",
        "unverified",
    }
