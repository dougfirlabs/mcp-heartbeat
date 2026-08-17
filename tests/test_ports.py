"""The adapter seams: structural ports and hints that carry no state.

The fake adapters below are the proof that the ports are implementable
without importing a transport — each satisfies its Protocol structurally,
with no base class and no MCP SDK anywhere in the file.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from mcp_heartbeat import (
    ChangeHint,
    FakeClock,
    HeartbeatIssuer,
    HeartbeatPublisher,
    HeartbeatSource,
    HintReceiver,
    IdentityBinder,
    IdentityBinding,
    IdentityClaim,
    InvalidHeartbeat,
    LineageState,
    admit,
)


class DictTransport:
    """A whole adapter in a dozen lines: publish, fetch, and hint."""

    def __init__(self) -> None:
        self.documents: dict[str, Mapping[str, Any]] = {}
        self.hints: list[ChangeHint] = []

    def publish(self, document: Mapping[str, Any]) -> None:
        self.documents[document["node_id"]] = document

    def fetch(self, participant_id: str) -> Mapping[str, Any]:
        return self.documents[participant_id]

    def on_hint(self, hint: ChangeHint) -> None:
        self.hints.append(hint)


class AllowlistBinder:
    """An adapter-owned binder. The policy table lives here, not in the core."""

    def __init__(self, principal: str | None, permitted: set[str]) -> None:
        self.principal = principal
        self.permitted = permitted

    def bind(self, claim: IdentityClaim) -> IdentityBinding:
        if self.principal is None:
            return IdentityBinding.UNVERIFIED
        if claim.participant_id in self.permitted:
            return IdentityBinding.BOUND
        return IdentityBinding.UNBOUND


@pytest.mark.parametrize(
    "port", [HeartbeatPublisher, HeartbeatSource, HintReceiver]
)
def test_a_plain_object_satisfies_the_ports_structurally(port) -> None:
    assert isinstance(DictTransport(), port)


def test_the_binder_port_is_satisfied_structurally() -> None:
    assert isinstance(AllowlistBinder(None, set()), IdentityBinder)


def test_an_adapter_drives_a_full_round_trip(  # the reference flow
) -> None:
    clock = FakeClock()
    transport = DictTransport()
    issuer = HeartbeatIssuer(participant_id="svc/api-7", epoch_id="e1", clock=clock)

    heartbeat = issuer.issue()
    transport.publish(heartbeat.to_dict())
    transport.on_hint(ChangeHint.for_heartbeat(heartbeat, address="mcp://svc/api-7"))

    state = LineageState(participant_id="svc/api-7")
    outcome = admit(state, transport.fetch("svc/api-7"), clock.now())
    assert outcome.accepted
    assert transport.hints[0].matches(outcome.state.held)


# ── a hint is a hint, never state ─────────────────────────────────────


def test_a_hint_carries_only_an_address_revision_and_digest() -> None:
    clock = FakeClock()
    heartbeat = HeartbeatIssuer(
        participant_id="p", epoch_id="e", clock=clock
    ).issue()
    hint = ChangeHint.for_heartbeat(heartbeat, address="mcp://p")
    assert set(hint.to_dict()) == {"address", "revision", "digest"}


def test_a_hint_round_trips():
    hint = ChangeHint(address="mcp://p", revision="e:1", digest="sha256:" + "ab" * 32)
    assert ChangeHint.from_dict(hint.to_dict()) == hint


def test_a_hint_missing_a_member_is_rejected() -> None:
    with pytest.raises(InvalidHeartbeat):
        ChangeHint.from_dict({"address": "mcp://p", "revision": "e:1"})


def test_a_hint_with_a_malformed_digest_is_rejected() -> None:
    with pytest.raises(InvalidHeartbeat):
        ChangeHint.from_dict({"address": "a", "revision": "r", "digest": "nope"})


def test_a_stale_hint_does_not_match_the_current_heartbeat() -> None:
    clock = FakeClock()
    issuer = HeartbeatIssuer(participant_id="p", epoch_id="e", clock=clock)
    stale = ChangeHint.for_heartbeat(issuer.issue(), address="mcp://p")
    assert not stale.matches(issuer.issue())


def test_a_hint_cannot_be_admitted_as_a_heartbeat() -> None:
    # Hints and heartbeats are different shapes on purpose: a consumer that
    # mistakes one for the other fails closed rather than inventing presence.
    hint = ChangeHint(address="mcp://p", revision="e:1", digest="sha256:" + "ab" * 32)
    outcome = admit(LineageState(participant_id="p"), hint.to_dict(), FakeClock().now())
    assert not outcome.accepted


# ── binding is the adapter's job, never the core's ────────────────────


def test_an_adapter_without_a_principal_reports_unverified() -> None:
    # Every gateway deployment lands here, and that is not an error.
    claim = IdentityClaim(participant_id="svc/api-7", epoch_id="e1")
    assert AllowlistBinder(None, {"svc/api-7"}).bind(claim) is IdentityBinding.UNVERIFIED


def test_an_adapter_binds_a_permitted_principal() -> None:
    claim = IdentityClaim(participant_id="svc/api-7", epoch_id="e1")
    binder = AllowlistBinder("spiffe://svc/api-7", {"svc/api-7"})
    assert binder.bind(claim) is IdentityBinding.BOUND


def test_an_adapter_refuses_a_principal_publishing_someone_else() -> None:
    claim = IdentityClaim(participant_id="svc/api-7", epoch_id="e1")
    binder = AllowlistBinder("spiffe://svc/other", {"svc/other"})
    assert binder.bind(claim) is IdentityBinding.UNBOUND


def test_binding_never_makes_the_claim_authenticated() -> None:
    claim = IdentityClaim(participant_id="svc/api-7", epoch_id="e1")
    bound = AllowlistBinder("spiffe://svc/api-7", {"svc/api-7"}).bind(claim)
    assert bound is IdentityBinding.BOUND
    assert IdentityClaim(
        participant_id="svc/api-7", epoch_id="e1", binding=bound
    ).authenticated is False


def test_admission_ignores_binding_entirely() -> None:
    # The core has no binder seam by construction: `admit` takes no binder
    # argument, so binding cannot influence a freshness verdict.
    import inspect

    assert "bind" not in inspect.signature(admit).parameters
