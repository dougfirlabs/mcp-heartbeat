"""Conformance against the pinned official SDK — MCP-HB-03-S1-T01.

These tests need ``mcp==2.0.0`` and ``mcp-types==2.0.0`` installed. They are
deliberately **not** run in a host application's shared virtualenv: installing the SDK
there resolves ``pydantic-core`` to a version other than the one the
installed ``pydantic`` pins, which would mutate an environment the rest of
the repository depends on for an adapter that does not need to live there.

So they skip by default and run in the isolated environment built by
``tools/verify_sdk.sh``, whose output is archived under
``docs/evidence/mcp-heartbeat-hb03/``. ``test_the_skip_is_only_ever_about_a_missing_sdk``
below keeps that honest: the suite may skip for exactly one reason, so a
skip can never quietly hide a failure.
"""
from __future__ import annotations

import json

import pytest

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.issuer import HeartbeatIssuer

from mcp_heartbeat_current import contract, sdk
from mcp_heartbeat_current.convergence import Convergence, FetchResult, HeartbeatConsumer
from mcp_heartbeat_current.discovery import HeartbeatCapability, negotiate
from mcp_heartbeat_current.subscriptions import SubscriptionFilter

pytestmark = pytest.mark.skipif(
    not sdk.SDK_AVAILABLE,
    reason="official mcp SDK not installed (see tools/verify_sdk.sh)",
)

PARTICIPANT = "svc/api-7"


def _publication():
    clock = FakeClock()
    issuer = HeartbeatIssuer(
        participant_id=PARTICIPANT, epoch_id="epoch-a", clock=clock, lease_seconds=30.0
    )
    publication = sdk.HeartbeatPublication(documents={})
    publication.put(issuer.issue())
    return publication, issuer, clock


# ── the guard that keeps a skip honest ────────────────────────────────


@pytest.mark.skipif(False, reason="always runs")
def test_the_skip_is_only_ever_about_a_missing_sdk() -> None:
    """Runs even without the SDK, so the skip reason cannot drift."""
    if sdk.SDK_AVAILABLE:
        assert sdk.sdk_provenance()["matches_pin"]
    else:
        with pytest.raises(sdk.SdkUnavailable) as excinfo:
            sdk.require_sdk()
        assert contract.SDK_PIN.version in str(excinfo.value)


# ── provenance and constant conformance ───────────────────────────────


def test_the_installed_sdk_matches_the_pin() -> None:
    provenance = sdk.sdk_provenance()
    assert provenance["installed"]["mcp"] == "2.0.0"
    assert provenance["installed"]["mcp-types"] == "2.0.0"
    assert provenance["matches_pin"]


def test_every_pinned_constant_still_matches_the_sdk() -> None:
    """The load-bearing check.

    Without it the pure layer would be a *copy* of the contract, and a copy
    decays silently. With it, the copy is a checked assertion.
    """
    conformance = sdk.assert_contract_matches_sdk()
    assert all(row["matches"] for row in conformance["constants"])
    assert len(conformance["constants"]) >= 18
    json.dumps(conformance)  # archivable as evidence


def test_the_extension_identifier_grammar_agrees_with_the_sdk() -> None:
    grammar = sdk.assert_contract_matches_sdk()["extension_identifier_grammar"]
    by_id = {row["identifier"]: row["valid"] for row in grammar}
    assert by_id[contract.HEARTBEAT_EXTENSION_ID] is True
    assert by_id["noslash"] is False


def test_the_sdk_accepts_our_extension_identifier_at_class_definition_time() -> None:
    """``Extension.__init_subclass__`` validates the id, so this cannot be faked."""
    extension = sdk.build_heartbeat_extension()
    assert extension.identifier == contract.HEARTBEAT_EXTENSION_ID
    assert extension.settings()["extension_version"] == "0.1"


# ── a real modern exchange ────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_real_client_discovers_the_extension_without_a_handshake() -> None:
    from mcp.client import Client

    publication, _, _ = _publication()
    server, _bus = sdk.build_heartbeat_server(publication)

    async with Client(server, mode=contract.PROTOCOL_REVISION) as client:
        raw = await sdk.discover_raw(client)
        assert contract.PROTOCOL_REVISION in raw["supportedVersions"]

        negotiated = negotiate(raw)
        assert negotiated.heartbeat_enabled
        assert negotiated.protocol_revision == contract.PROTOCOL_REVISION
        assert negotiated.extension_version == "0.1"


@pytest.mark.anyio
async def test_a_real_client_reads_the_authoritative_lease() -> None:
    from mcp.client import Client

    publication, issuer, clock = _publication()
    server, _bus = sdk.build_heartbeat_server(publication)

    async with Client(server, mode=contract.PROTOCOL_REVISION) as client:
        negotiated = await sdk.discover(client)
        document = await sdk.read_heartbeat(client, negotiated, PARTICIPANT)
        assert document["node_id"] == PARTICIPANT
        assert document["sequence"] == 0

        clock.advance(1.0)
        publication.put(issuer.issue())
        refetched = await sdk.read_heartbeat(client, negotiated, PARTICIPANT)
        assert refetched["sequence"] == 1, "cache_mode=bypass, or this would be stale"


@pytest.mark.anyio
async def test_the_portable_core_admits_a_document_fetched_over_real_mcp() -> None:
    """End to end: SDK transport in, core admission out, no MCP in the core."""
    from mcp.client import Client

    publication, issuer, clock = _publication()
    server, _bus = sdk.build_heartbeat_server(publication)

    async with Client(server, mode=contract.PROTOCOL_REVISION) as client:
        negotiated = await sdk.discover(client)
        fetched = await sdk.fetch_result(client, negotiated, PARTICIPANT)
        assert isinstance(fetched, FetchResult)

        class OneShot:
            def fetch(self, participant_id: str) -> FetchResult:
                return fetched

        consumer = HeartbeatConsumer(PARTICIPANT, OneShot(), clock)
        assert consumer.refetch().convergence is Convergence.ADVANCED
        assert consumer.held is not None and consumer.held.participant_id == PARTICIPANT


@pytest.mark.anyio
async def test_a_real_subscription_acknowledges_before_it_notifies() -> None:
    from mcp.client import Client
    from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY

    assert SUBSCRIPTION_ID_META_KEY == contract.SUBSCRIPTION_ID_META_KEY

    publication, issuer, clock = _publication()
    server, _bus = sdk.build_heartbeat_server(publication)

    async with Client(server, mode=contract.PROTOCOL_REVISION) as client:
        negotiated = await sdk.discover(client)
        uri = negotiated.capability.resource_uri(PARTICIPANT)
        async with client.listen(resource_subscriptions=[uri]) as subscription:
            # Entering the context blocks until the acknowledgement, so a
            # subscription id existing here *is* the ack-first guarantee.
            assert subscription.subscription_id is not None
            assert uri in subscription.honored.resource_subscriptions


def test_the_listen_filter_asks_only_for_the_tracked_participants() -> None:
    negotiated = negotiate(
        {
            "supportedVersions": [contract.PROTOCOL_REVISION],
            "capabilities": {
                "extensions": {contract.HEARTBEAT_EXTENSION_ID: HeartbeatCapability().to_dict()}
            },
        }
    )
    subscription_filter = sdk.listen_filter(negotiated, [PARTICIPANT, "svc/api-8"])
    assert isinstance(subscription_filter, SubscriptionFilter)
    assert subscription_filter.resource_subscriptions == (
        "heartbeat://participants/svc%2Fapi-7",
        "heartbeat://participants/svc%2Fapi-8",
    )
    assert not subscription_filter.tools_list_changed
