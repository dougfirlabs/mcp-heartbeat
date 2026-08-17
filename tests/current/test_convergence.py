"""Authoritative refetch and convergence — MCP-HB-03-S2-T02."""
from __future__ import annotations

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.errors import ViolationCode
from mcp_heartbeat.ports import ChangeHint

from mcp_heartbeat_current.convergence import Convergence, HeartbeatConsumer

from hb_current_testkit import ADDRESS, PARTICIPANT, Publisher, ScriptedSource


def _hint(publisher: Publisher) -> ChangeHint:
    assert publisher.latest is not None
    return ChangeHint.for_heartbeat(publisher.latest, address=ADDRESS)


# ── the happy path ────────────────────────────────────────────────────


def test_the_first_refetch_establishes_the_lease(
    consumer: HeartbeatConsumer, publisher: Publisher
) -> None:
    publisher.publish()
    verdict = consumer.refetch()
    assert verdict.convergence is Convergence.ADVANCED
    assert verdict.authoritative
    assert consumer.held is not None
    assert consumer.held.sequence == 0


def test_a_hint_schedules_a_refetch_and_the_refetch_decides(
    consumer: HeartbeatConsumer, publisher: Publisher
) -> None:
    publisher.publish()
    consumer.refetch()
    publisher.publish()

    assert consumer.on_hint(_hint(publisher)) is True
    assert consumer.refetch_pending
    assert consumer.refetch().convergence is Convergence.ADVANCED
    assert consumer.held is not None and consumer.held.sequence == 1
    assert not consumer.refetch_pending


# ── loss tolerance ────────────────────────────────────────────────────


def test_a_consumer_that_receives_no_hint_at_all_still_converges(
    consumer: HeartbeatConsumer, publisher: Publisher, clock: FakeClock
) -> None:
    """The acceptance criterion, and the reason delivery is an optimisation.

    The refetch deadline comes from the held lease's own expiry, so a
    consumer whose hint channel is entirely dead is late, never wrong.
    """
    publisher.publish()
    consumer.refetch()

    # The producer keeps renewing on its own schedule; every hint is lost.
    for _ in range(4):
        clock.advance(5.0)
        publisher.publish()

    assert consumer.hints_seen == 0
    assert consumer.due()
    verdict = consumer.poll()
    assert verdict is not None and verdict.convergence is Convergence.ADVANCED
    assert consumer.held is not None and consumer.held.sequence == 4


def test_missed_notifications_cannot_fabricate_state(
    consumer: HeartbeatConsumer, publisher: Publisher
) -> None:
    """A hint alone never moves held state."""
    publisher.publish()
    consumer.refetch()
    held = consumer.held

    publisher.publish()
    consumer.on_hint(_hint(publisher))
    assert consumer.held is held, "the hint changed nothing until the refetch"


def test_missed_notifications_cannot_permanently_hide_state(
    consumer: HeartbeatConsumer, publisher: Publisher, clock: FakeClock
) -> None:
    publisher.publish()
    consumer.refetch()
    clock.advance(20.0)
    publisher.publish()
    assert consumer.poll() is not None
    assert consumer.held is not None and consumer.held.sequence == 1


# ── idempotence ───────────────────────────────────────────────────────


def test_a_duplicate_hint_costs_nothing(
    consumer: HeartbeatConsumer, publisher: Publisher
) -> None:
    publisher.publish()
    consumer.refetch()
    hint = _hint(publisher)

    assert consumer.on_hint(hint) is False, "the hint matches what we hold"
    assert consumer.on_hint(hint) is False
    assert not consumer.refetch_pending
    assert consumer.hints_seen == 2


def test_redelivering_the_held_revision_is_unchanged_not_advanced(
    consumer: HeartbeatConsumer, publisher: Publisher
) -> None:
    publisher.publish()
    consumer.refetch()
    verdict = consumer.refetch()
    assert verdict.convergence is Convergence.UNCHANGED
    assert not verdict.authoritative


def test_a_reordered_hint_cannot_roll_the_sequence_backward(
    consumer: HeartbeatConsumer, publisher: Publisher
) -> None:
    """Acceptance: reordered hints cannot roll sequence backward."""
    first = publisher.publish()
    consumer.refetch()
    publisher.publish()
    consumer.refetch()
    assert consumer.held is not None and consumer.held.sequence == 1

    stale = ChangeHint.for_heartbeat(first, address=ADDRESS)
    consumer.on_hint(stale)
    publisher.documents[PARTICIPANT] = first.to_dict()  # a replica serves the old revision
    verdict = consumer.refetch()

    assert verdict.convergence is Convergence.REFUSED
    assert verdict.reason is ViolationCode.SEQUENCE_ROLLBACK
    assert consumer.held.sequence == 1, "the held lease is preserved"


def test_a_forged_hint_cannot_promote_itself(
    consumer: HeartbeatConsumer, publisher: Publisher
) -> None:
    publisher.publish()
    consumer.refetch()
    forged = ChangeHint(
        address=ADDRESS, revision="epoch-a:99", digest="sha256:" + "ff" * 32
    )
    consumer.on_hint(forged)
    assert consumer.refetch_pending, "it only earns a refetch"
    assert consumer.refetch().convergence is Convergence.UNCHANGED
    assert consumer.held is not None and consumer.held.sequence == 0


# ── the fetch itself failing ──────────────────────────────────────────


def test_an_unreachable_source_preserves_the_held_lease(
    consumer: HeartbeatConsumer, publisher: Publisher, source: ScriptedSource
) -> None:
    publisher.publish()
    consumer.refetch()
    held = consumer.held

    source.failures = 1
    verdict = consumer.refetch()
    assert verdict.convergence is Convergence.UNREACHABLE
    assert consumer.held is held


def test_recovery_after_a_disconnection_converges(
    consumer: HeartbeatConsumer, publisher: Publisher, source: ScriptedSource, clock: FakeClock
) -> None:
    publisher.publish()
    consumer.refetch()
    source.failures = 2
    assert consumer.refetch().convergence is Convergence.UNREACHABLE
    assert consumer.refetch().convergence is Convergence.UNREACHABLE

    publisher.publish()
    clock.advance(1.0)
    assert consumer.refetch().convergence is Convergence.ADVANCED


def test_expiry_is_decided_by_the_held_lease_not_by_silence(
    consumer: HeartbeatConsumer, publisher: Publisher, source: ScriptedSource, clock: FakeClock
) -> None:
    publisher.publish()
    consumer.refetch()
    assert consumer.is_fresh()

    source.failures = 99
    clock.advance(31.0)
    consumer.refetch()
    assert not consumer.is_fresh(), "the lease expired on its own schedule"
    assert consumer.held is not None, "and is retained, so the last-known state is readable"


# ── scheduling ────────────────────────────────────────────────────────


def test_a_consumer_with_nothing_held_is_always_due(
    consumer: HeartbeatConsumer,
) -> None:
    assert consumer.refetch_deadline() is None
    assert consumer.due()


def test_the_deadline_falls_inside_the_held_window(
    consumer: HeartbeatConsumer, publisher: Publisher, clock: FakeClock
) -> None:
    """Refetching while still valid is what makes one lost hint a non-event."""
    heartbeat = publisher.publish()
    consumer.refetch()
    deadline = consumer.refetch_deadline()
    assert deadline is not None
    assert clock.now() < deadline < heartbeat.expires_at


def test_poll_is_a_no_op_before_the_deadline(
    consumer: HeartbeatConsumer, publisher: Publisher, source: ScriptedSource
) -> None:
    publisher.publish()
    consumer.refetch()
    fetches = source.fetches
    assert consumer.poll() is None
    assert source.fetches == fetches
