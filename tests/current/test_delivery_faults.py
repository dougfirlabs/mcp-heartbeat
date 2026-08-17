"""The delivery-fault matrix — MCP-HB-03-S2-T03.

Each deployment topology named in the PRD gets a test that drives it to a
verdict, and the verdict is compared against
:data:`mcp_heartbeat_current.convergence.TOPOLOGY_RULES`. Pinning the table
rather than the assertion alone is what makes "deterministic" checkable: a
refactor that quietly changes an outcome fails a test that names the
topology, instead of passing with a different-but-plausible answer.

The matrix these produce is exported as the PRD's "subscription fault
matrix" evidence by ``tools/emit_evidence.py``.
"""
from __future__ import annotations

import pytest

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.errors import ViolationCode
from mcp_heartbeat.issuer import HeartbeatIssuer
from mcp_heartbeat.model import IdentityBinding
from mcp_heartbeat.ports import ChangeHint

from mcp_heartbeat_current.convergence import (
    TOPOLOGY_BY_NAME,
    TOPOLOGY_RULES,
    Convergence,
    HeartbeatConsumer,
)
from mcp_heartbeat_current.identity import Principal, RequestIdentityBinder

from hb_current_testkit import ADDRESS, PARTICIPANT, Publisher, ScriptedSource



def _assert_matches_rule(topology: str, verdict) -> None:
    rule = TOPOLOGY_BY_NAME[topology]
    assert verdict.convergence is rule.verdict, (
        f"{topology}: expected {rule.verdict.value}, got {verdict.convergence.value}"
    )
    if rule.reason is not None:
        assert str(verdict.reason) == rule.reason


def test_the_table_covers_every_topology_the_prd_names() -> None:
    assert {rule.topology for rule in TOPOLOGY_RULES} == {
        "round_robin_replicas",
        "gateway_termination",
        "serverless_cold_start",
        "rolling_deployment",
        "asymmetric_connectivity",
        "backpressure",
    }


# ── round-robin replicas ──────────────────────────────────────────────


def test_round_robin_replicas_surface_the_split_instead_of_alternating(
    consumer: HeartbeatConsumer, publisher: Publisher, clock: FakeClock
) -> None:
    """Two live replicas answering under one participant id.

    Each has its own epoch, so alternating reads would look like two
    plausible lease streams. The flap back to the already-retired epoch is
    refused, which is what makes the misconfiguration visible.
    """
    replica_a = publisher.publish()
    assert consumer.refetch().convergence is Convergence.ADVANCED

    # The next read is answered by the other replica: same participant,
    # different epoch.
    other = HeartbeatIssuer(
        participant_id=PARTICIPANT, epoch_id="epoch-b", clock=clock, lease_seconds=30.0
    )
    publisher.documents[PARTICIPANT] = other.issue().to_dict()
    assert consumer.refetch().convergence is Convergence.ADVANCED

    # ...and back to the first replica.
    publisher.documents[PARTICIPANT] = replica_a.to_dict()
    verdict = consumer.refetch()

    _assert_matches_rule("round_robin_replicas", verdict)
    assert verdict.reason is ViolationCode.BOOT_ID_REUSE
    assert consumer.held is not None and consumer.held.epoch_id == "epoch-b"


# ── gateway termination ───────────────────────────────────────────────


def test_gateway_termination_binds_per_response_not_per_channel(
    publisher: Publisher, source: ScriptedSource, clock: FakeClock, binder_factory, principal
) -> None:
    """One authenticated channel, many participants, separate verdicts."""
    publisher.publish()
    source.principal = principal

    tracked = HeartbeatConsumer(
        PARTICIPANT, source, clock, binder_factory=binder_factory
    )
    verdict = tracked.refetch()
    _assert_matches_rule("gateway_termination", verdict)
    assert verdict.binding is not None
    assert verdict.binding.binding is IdentityBinding.BOUND

    # The same channel, the same principal, a participant it does not hold.
    impostor = HeartbeatConsumer("svc/api-8", source, clock, binder_factory=binder_factory)
    publisher.documents["svc/api-8"] = publisher.documents[PARTICIPANT]
    other = impostor.refetch()
    assert other.convergence is Convergence.IDENTITY_UNBOUND
    assert impostor.held is None, "fails closed"


def test_an_unauthenticated_gateway_read_is_retained_but_never_authoritative(
    publisher: Publisher, source: ScriptedSource, clock: FakeClock, binder_factory
) -> None:
    publisher.publish()
    source.principal = None
    consumer = HeartbeatConsumer(PARTICIPANT, source, clock, binder_factory=binder_factory)

    verdict = consumer.refetch()
    assert verdict.convergence is Convergence.NON_AUTHORITATIVE
    assert verdict.binding is not None
    assert verdict.binding.binding is IdentityBinding.UNVERIFIED
    assert consumer.held is None, "readable, but it may not move held state"


# ── serverless cold start ─────────────────────────────────────────────


def test_a_cold_start_is_a_new_stream_not_a_rollback(
    consumer: HeartbeatConsumer, publisher: Publisher, clock: FakeClock
) -> None:
    publisher.publish()
    publisher.publish()
    clock.advance(1.0)
    consumer.refetch()
    assert consumer.held is not None and consumer.held.sequence == 1

    clock.advance(5.0)
    publisher.restart("epoch-cold-1")
    publisher.publish()  # sequence back to 0

    verdict = consumer.refetch()
    _assert_matches_rule("serverless_cold_start", verdict)
    assert consumer.held.sequence == 0
    assert consumer.held.epoch_id == "epoch-cold-1"


def test_a_cold_start_that_reuses_a_retired_epoch_is_refused(
    consumer: HeartbeatConsumer, publisher: Publisher, clock: FakeClock
) -> None:
    """Why epochs are random, never derived from hostname or config."""
    publisher.publish()
    consumer.refetch()
    clock.advance(5.0)
    publisher.restart("epoch-b")
    publisher.publish()
    consumer.refetch()

    clock.advance(5.0)
    publisher.restart("epoch-a")  # the same id a previous boot already used
    publisher.publish()
    assert consumer.refetch().reason is ViolationCode.BOOT_ID_REUSE


# ── rolling deployment ────────────────────────────────────────────────


def test_a_superseded_instance_cannot_keep_publishing(
    consumer: HeartbeatConsumer, publisher: Publisher, clock: FakeClock
) -> None:
    old = HeartbeatIssuer(
        participant_id=PARTICIPANT, epoch_id="epoch-old", clock=clock, lease_seconds=30.0
    )
    publisher.documents[PARTICIPANT] = old.issue().to_dict()
    consumer.refetch()

    clock.advance(2.0)
    new = HeartbeatIssuer(
        participant_id=PARTICIPANT, epoch_id="epoch-new", clock=clock, lease_seconds=30.0
    )
    publisher.documents[PARTICIPANT] = new.issue().to_dict()
    assert consumer.refetch().convergence is Convergence.ADVANCED

    clock.advance(2.0)
    publisher.documents[PARTICIPANT] = old.issue().to_dict()  # the old pod is still up
    verdict = consumer.refetch()

    _assert_matches_rule("rolling_deployment", verdict)
    assert consumer.held is not None and consumer.held.epoch_id == "epoch-new"


# ── asymmetric connectivity ───────────────────────────────────────────


def test_hints_arriving_while_reads_fail_fabricate_nothing(
    consumer: HeartbeatConsumer, publisher: Publisher, source: ScriptedSource, clock: FakeClock
) -> None:
    """The hint path is up, the read path is down. Nothing is inferred."""
    publisher.publish()
    consumer.refetch()
    held = consumer.held

    source.failures = 5
    for sequence in range(1, 4):
        clock.advance(1.0)
        publisher.publish()
        consumer.on_hint(
            ChangeHint(address=ADDRESS, revision=f"epoch-a:{sequence}", digest="sha256:" + "cd" * 32)
        )
        verdict = consumer.refetch()
        _assert_matches_rule("asymmetric_connectivity", verdict)

    assert consumer.held is held, "three hints, zero movement"


def test_the_lease_still_expires_on_its_own_schedule_under_asymmetry(
    consumer: HeartbeatConsumer, publisher: Publisher, source: ScriptedSource, clock: FakeClock
) -> None:
    publisher.publish()
    consumer.refetch()
    source.failures = 99
    clock.advance(31.0)
    consumer.on_hint(ChangeHint(address=ADDRESS, revision="epoch-a:9", digest="sha256:" + "ef" * 32))
    consumer.refetch()
    assert not consumer.is_fresh()


# ── backpressure ──────────────────────────────────────────────────────


def test_hints_coalesce_so_correctness_is_independent_of_the_drop_rate(
    consumer: HeartbeatConsumer, publisher: Publisher, source: ScriptedSource, clock: FakeClock
) -> None:
    publisher.publish()
    consumer.refetch()
    fetches_before = source.fetches

    for sequence in range(1, 51):
        clock.advance(0.1)
        publisher.publish()
        consumer.on_hint(
            ChangeHint(address=ADDRESS, revision=f"epoch-a:{sequence}", digest="sha256:" + "12" * 32)
        )

    assert consumer.hints_seen == 50
    assert consumer.hints_coalesced == 49, "fifty hints, one pending refetch"

    verdict = consumer.refetch()
    _assert_matches_rule("backpressure", verdict)
    assert source.fetches == fetches_before + 1
    assert consumer.held is not None and consumer.held.sequence == 50


@pytest.mark.parametrize("drop_rate", [0, 1, 5, 49, 50])
def test_the_final_state_is_the_same_however_many_hints_are_dropped(
    drop_rate: int, publisher: Publisher, source: ScriptedSource, clock: FakeClock
) -> None:
    """The convergence claim, swept: delivery changes latency, not outcome."""
    consumer = HeartbeatConsumer(PARTICIPANT, source, clock)
    publisher.publish()
    consumer.refetch()

    for sequence in range(1, 51):
        clock.advance(0.1)
        publisher.publish()
        if sequence > drop_rate:
            consumer.on_hint(
                ChangeHint(
                    address=ADDRESS, revision=f"epoch-a:{sequence}", digest="sha256:" + "12" * 32
                )
            )

    consumer.refetch()
    assert consumer.held is not None and consumer.held.sequence == 50


# ── the table is the contract ─────────────────────────────────────────


def test_every_rule_states_a_situation_and_a_deterministic_verdict() -> None:
    for rule in TOPOLOGY_RULES:
        assert rule.situation and rule.rule
        assert isinstance(rule.verdict, Convergence)
