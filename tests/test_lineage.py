"""Admission determinism: restart, monotonicity, duplicate, rollback, conflict,
expiry, and bounded skew.

Every case here runs on a :class:`FakeClock`, so none of it sleeps and none
of it can flake. That is the point of injecting the clock.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from mcp_heartbeat import (
    FakeClock,
    Heartbeat,
    HeartbeatIssuer,
    LineageState,
    ViolationCode,
    admit,
    check_freshness,
    is_duplicate,
)

PARTICIPANT = "svc/api-7"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state() -> LineageState:
    return LineageState(participant_id=PARTICIPANT)


def issuer_for(clock: FakeClock, epoch: str = "epoch-1") -> HeartbeatIssuer:
    return HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id=epoch, clock=clock)


def accept(state: LineageState, heartbeat: Heartbeat, clock: FakeClock) -> LineageState:
    outcome = admit(state, heartbeat.to_dict(), clock.now())
    assert outcome.accepted, outcome.reason
    return outcome.state


# ── the happy path ────────────────────────────────────────────────────


def test_the_first_heartbeat_of_a_participant_is_admitted(state, clock) -> None:
    outcome = admit(state, issuer_for(clock).issue().to_dict(), clock.now())
    assert outcome.accepted
    assert outcome.state.held is not None
    assert outcome.state.retired_epochs == frozenset({"epoch-1"})


def test_a_monotonic_sequence_within_one_epoch_is_admitted(state, clock) -> None:
    issuer = issuer_for(clock)
    for _ in range(5):
        state = accept(state, issuer.issue(), clock)
        clock.advance(1)
    assert state.held is not None and state.held.sequence == 4


# ── restart ───────────────────────────────────────────────────────────


def test_a_restart_under_a_fresh_epoch_is_admitted_and_resets_the_counter(
    state, clock
) -> None:
    state = accept(state, issuer_for(clock, "epoch-1").issue(), clock)
    clock.advance(1)

    restarted = issuer_for(clock, "epoch-2")
    state = accept(state, restarted.issue(), clock)

    assert state.held is not None
    assert state.held.epoch_id == "epoch-2"
    assert state.held.sequence == 0  # a new epoch restarts the counter
    assert state.retired_epochs == frozenset({"epoch-1", "epoch-2"})


def test_a_retired_epoch_may_never_reappear(state, clock) -> None:
    # A rollback that reuses its old epoch is indistinguishable from a
    # replay, so it fails closed.
    first = issuer_for(clock, "epoch-1")
    state = accept(state, first.issue(), clock)
    clock.advance(1)
    state = accept(state, issuer_for(clock, "epoch-2").issue(), clock)
    clock.advance(1)

    outcome = admit(state, first.issue().to_dict(), clock.now())
    assert outcome.reason is ViolationCode.BOOT_ID_REUSE
    assert outcome.state.held is state.held  # held state is preserved


# ── rollback, duplicate, conflict ─────────────────────────────────────


def test_a_lower_sequence_in_the_held_epoch_is_a_rollback(state, clock) -> None:
    issuer = issuer_for(clock)
    replayed = issuer.issue()
    clock.advance(1)
    state = accept(state, issuer.issue(), clock)

    outcome = admit(state, replayed.to_dict(), clock.now())
    assert outcome.reason is ViolationCode.SEQUENCE_ROLLBACK


def test_byte_identical_redelivery_is_a_duplicate_not_a_violation(
    state, clock
) -> None:
    heartbeat = issuer_for(clock).issue()
    state = accept(state, heartbeat, clock)

    outcome = admit(state, heartbeat.to_dict(), clock.now())
    assert outcome.duplicate is True
    assert outcome.reason is None
    assert outcome.accepted is False  # idempotent, so not a transition either
    assert outcome.state is state
    assert is_duplicate(state, heartbeat)


def test_the_same_counter_with_different_bytes_is_a_conflict(state, clock) -> None:
    # Two writers sharing one participant id: the interleaving is exactly what
    # a replica deployment that reuses a node_id produces.
    issuer = issuer_for(clock)
    state = accept(state, issuer.issue(), clock)

    rival = HeartbeatIssuer(
        participant_id=PARTICIPANT, epoch_id="epoch-1", clock=clock, lease_seconds=45
    )
    outcome = admit(state, rival.issue().to_dict(), clock.now())
    assert outcome.reason is ViolationCode.SEQUENCE_CONFLICT


# ── addressing ────────────────────────────────────────────────────────


def test_a_document_for_another_participant_is_refused(state, clock) -> None:
    other = HeartbeatIssuer(participant_id="svc/other", epoch_id="e", clock=clock)
    outcome = admit(state, other.issue().to_dict(), clock.now())
    assert outcome.reason is ViolationCode.NODE_ID_MISMATCH


# ── expiry and skew ───────────────────────────────────────────────────


def test_a_heartbeat_that_already_expired_is_refused(state, clock) -> None:
    heartbeat = issuer_for(clock).issue()
    clock.advance(31)
    outcome = admit(state, heartbeat.to_dict(), clock.now())
    # Skew is bounded more tightly than the window, so a lease this stale
    # trips skew first; both are refusals and neither mutates state.
    assert outcome.reason in {
        ViolationCode.EXPIRED_ON_ARRIVAL,
        ViolationCode.CLOCK_SKEW_EXCEEDED,
    }
    assert outcome.state.held is None


def test_expiry_is_named_when_skew_is_permitted(state, clock) -> None:
    heartbeat = issuer_for(clock).issue()
    clock.advance(31)
    outcome = admit(state, heartbeat.to_dict(), clock.now(), max_skew_seconds=120)
    assert outcome.reason is ViolationCode.EXPIRED_ON_ARRIVAL


def test_a_heartbeat_issued_too_far_from_now_is_refused(state, clock) -> None:
    heartbeat = issuer_for(clock).issue()
    clock.skew_wall(10)  # wall clock steps; monotonic time does not
    outcome = admit(state, heartbeat.to_dict(), clock.now())
    assert outcome.reason is ViolationCode.CLOCK_SKEW_EXCEEDED


def test_skew_within_the_bound_is_tolerated(state, clock) -> None:
    heartbeat = issuer_for(clock).issue()
    clock.skew_wall(4)
    assert admit(state, heartbeat.to_dict(), clock.now()).accepted


def test_skew_is_bounded_in_both_directions(clock) -> None:
    heartbeat = issuer_for(clock).issue()
    future = clock.now() - timedelta(seconds=10)  # heartbeat from the "future"
    assert check_freshness(heartbeat, future) is ViolationCode.CLOCK_SKEW_EXCEEDED


# ── ordering is normative ─────────────────────────────────────────────


def test_lineage_is_evaluated_before_skew(state, clock) -> None:
    # A replayed old revision is also stale. Checking skew first would report
    # every replay as `clock_skew_exceeded` and hide the rollback.
    issuer = issuer_for(clock)
    replayed = issuer.issue()
    clock.advance(1)
    state = accept(state, issuer.issue(), clock)
    clock.advance(600)  # now the replay is stale *and* a rollback

    outcome = admit(state, replayed.to_dict(), clock.now())
    assert outcome.reason is ViolationCode.SEQUENCE_ROLLBACK


def test_structure_is_evaluated_before_lineage(state, clock) -> None:
    state = accept(state, issuer_for(clock).issue(), clock)
    outcome = admit(state, {"extension_version": "0.1"}, clock.now())
    assert outcome.reason is ViolationCode.SCHEMA_INVALID


# ── failing closed ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d | {"sequence": -1},
        lambda d: d | {"node_id": "svc/other"},
        lambda d: d | {"extension_version": "0.2"},
        lambda d: {},
    ],
)
def test_every_refusal_preserves_the_held_heartbeat(state, clock, mutate) -> None:
    state = accept(state, issuer_for(clock).issue(), clock)
    held = state.held

    outcome = admit(state, mutate(issuer_for(clock).issue().to_dict()), clock.now())
    assert not outcome.accepted
    assert outcome.state.held is held
    assert outcome.state.retired_epochs == state.retired_epochs


def test_optional_data_cannot_change_a_freshness_verdict(state, clock) -> None:
    # "Safely ignorable" as a test: discarding every extension reaches the
    # same verdict, so optional data can never buy admission.
    issuer = HeartbeatIssuer(
        participant_id=PARTICIPANT,
        epoch_id="e1",
        clock=clock,
        extensions={"org.example.pressure": 0.99, "org.example.health": "unhealthy"},
    )
    decorated = issuer.issue()
    stripped = Heartbeat(
        node_id=decorated.node_id,
        boot_id=decorated.boot_id,
        sequence=decorated.sequence,
        issued_at=decorated.issued_at,
        expires_at=decorated.expires_at,
    )
    now = clock.now()
    assert admit(state, decorated.to_dict(), now).accepted
    assert admit(state, stripped.to_dict(), now).accepted
    assert check_freshness(decorated, now) == check_freshness(stripped, now)
