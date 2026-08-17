"""Identity and epoch behaviour across real deployment shapes.

The independent review's second insight was that a heartbeat is easy to
get right against one process and easy to get wrong against a fleet. So
this matrix drives the *real* :class:`~mcp_heartbeat_current.convergence.HeartbeatConsumer`
through the shapes a production deployment actually has — replicas
behind a gateway, round-robin routing, cold starts, rolling deploys,
one-way connectivity, suspension, backpressure, and partitions — and
records a deterministic verdict for each.

Two things make these cases honest rather than decorative:

* **Nothing sleeps.** Every case runs on :class:`~mcp_heartbeat.clock.FakeClock`,
  so a partition that lasts three lease windows costs no wall-clock time
  and the result cannot flake on a loaded machine.
* **The failure mode is asserted, not just the absence of a crash.**
  Round-robin does not merely "not converge wrongly": it produces
  ``sequence_conflict`` or ``boot_id_reuse``, and the case says which.

The shapes here are the fleet cases. Era classification is
:mod:`.cross_era`; identity binding is :mod:`.identity_matrix`.
"""
from __future__ import annotations

from typing import Any, Mapping

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.issuer import HeartbeatIssuer
from mcp_heartbeat.ports import ChangeHint
from mcp_heartbeat_current.convergence import (
    AuthoritativeSource,
    Convergence,
    FetchResult,
    HeartbeatConsumer,
)

from .verdicts import Case, MatrixReport, run_cases

PARTICIPANT = "svc/api-7"
LEASE_SECONDS = 30.0


class Replica:
    """One serving process: its own epoch, its own monotonic counter.

    Replicas share a participant id — that is the whole difficulty. Two
    of them answering under one name is not a bug in the fleet, it is
    the normal shape of a load-balanced service, and the consumer has to
    survive it without inventing a merged history.
    """

    def __init__(self, epoch_id: str, clock: FakeClock, *, lease_seconds: float = LEASE_SECONDS):
        self.epoch_id = epoch_id
        self.issuer = HeartbeatIssuer(
            participant_id=PARTICIPANT,
            epoch_id=epoch_id,
            clock=clock,
            lease_seconds=lease_seconds,
        )
        self.current = self.issuer.issue()

    def beat(self) -> Mapping[str, Any]:
        """Advance this replica's own lease and return the document."""
        self.current = self.issuer.issue()
        return self.current.to_dict()

    def document(self) -> Mapping[str, Any]:
        return self.current.to_dict()


class Gateway(AuthoritativeSource):
    """A routing front end that answers from whichever replica it picks.

    Deliberately dumb: it has no idea the backends disagree, which is
    exactly the property that makes it dangerous and worth testing
    against. ``unreachable`` models a partition or a dead pool.
    """

    def __init__(self, *replicas: Replica) -> None:
        self.replicas = list(replicas)
        self.cursor = 0
        self.unreachable = False
        self.pinned: Replica | None = None
        self.served: list[str] = []

    def route(self) -> Replica:
        if self.pinned is not None:
            return self.pinned
        replica = self.replicas[self.cursor % len(self.replicas)]
        self.cursor += 1
        return replica

    def fetch(self, participant_id: str) -> FetchResult:
        if self.unreachable:
            raise ConnectionError("no backend reachable")
        replica = self.route()
        self.served.append(replica.epoch_id)
        return FetchResult(document=replica.document())


def _consumer(source: AuthoritativeSource, clock: FakeClock) -> HeartbeatConsumer:
    return HeartbeatConsumer(PARTICIPANT, source, clock)


# ── the cases ─────────────────────────────────────────────────────────


def replicas_keep_distinct_epochs(case: Case) -> None:
    """Two replicas under one name never merge into one history.

    The dangerous outcome is a consumer that splices replica A's
    sequence onto replica B's and reports a lease that no process ever
    issued. It must instead hold exactly one epoch at a time.
    """
    clock = FakeClock()
    a, b = Replica("epoch-a", clock), Replica("epoch-b", clock)
    gateway = Gateway(a)
    consumer = _consumer(gateway, clock)

    case.check("first_epoch_admitted", consumer.refetch().convergence is Convergence.ADVANCED)
    held_a = consumer.held
    case.check("holding_epoch_a", held_a is not None and held_a.boot_id == "epoch-a")

    # The gateway starts answering from the other replica entirely — a
    # failover, not a restart of the same process.
    gateway.replicas = [b]
    gateway.pinned = b
    clock.advance(1.0)
    b.beat()
    verdict = consumer.refetch()

    case.check(
        "failover_to_a_second_epoch_is_admitted",
        verdict.convergence is Convergence.ADVANCED,
        verdict,
    )
    held_b = consumer.held
    case.check("now_holding_epoch_b", held_b is not None and held_b.boot_id == "epoch-b")
    case.check(
        "epoch_a_was_retired_not_merged",
        "epoch-a" in consumer.state.retired_epochs,
        sorted(consumer.state.retired_epochs),
    )
    case.observations = {
        "held_epoch": held_b.boot_id if held_b else None,
        "retired": sorted(consumer.state.retired_epochs),
    }


def round_robin_replay_is_refused(case: Case) -> None:
    """Round-robin routing must not let a retired epoch come back.

    A load balancer alternating between two replicas will, sooner or
    later, hand back the epoch the consumer already moved off. Accepting
    it would let a stale process masquerade as the current one, so the
    refusal has to be named — ``boot_id_reuse`` — not merely implied by
    the sequence not moving.
    """
    clock = FakeClock()
    a, b = Replica("epoch-a", clock), Replica("epoch-b", clock)
    gateway = Gateway(a)
    consumer = _consumer(gateway, clock)
    consumer.refetch()

    gateway.pinned = b
    clock.advance(1.0)
    b.beat()
    case.check("moved_to_epoch_b", consumer.refetch().convergence is Convergence.ADVANCED)

    # The balancer swings back to the retired replica.
    gateway.pinned = a
    clock.advance(1.0)
    a.beat()
    verdict = consumer.refetch()

    case.check(
        "retired_epoch_is_refused",
        verdict.convergence is Convergence.REFUSED,
        verdict,
    )
    case.check("refusal_names_boot_id_reuse", str(verdict.reason) == "boot_id_reuse", verdict.reason)
    held = consumer.held
    case.check("still_holding_the_current_epoch", held is not None and held.boot_id == "epoch-b")
    case.observations = {"verdict": verdict, "served": gateway.served}


def split_brain_is_a_conflict_not_a_merge(case: Case) -> None:
    """Two writers at the same counter is reported, never averaged.

    This is the case a naive "highest sequence wins" consumer gets
    wrong: same epoch id, same sequence, different bytes means two
    processes believe they are the same one.
    """
    clock = FakeClock()
    a = Replica("epoch-a", clock)
    gateway = Gateway(a)
    consumer = _consumer(gateway, clock)
    consumer.refetch()
    held = consumer.held
    case.check("holding_the_first_beat", held is not None and held.sequence == 0)

    # A second process wakes up believing it *is* epoch-a and starts its
    # own counter at zero. Same epoch id, same sequence, different bytes
    # — because it minted its lease a moment later.
    clock.advance(0.5)
    twin = Replica("epoch-a", clock)
    case.check(
        "the_twin_collides_on_epoch_and_sequence",
        twin.current.boot_id == (held.boot_id if held else None)
        and twin.current.sequence == (held.sequence if held else None),
        {"twin": twin.current.revision, "held": held.revision if held else None},
    )
    case.check(
        "but_disagrees_on_content",
        twin.current.digest != (held.digest if held else None),
    )

    gateway.pinned = twin
    verdict = consumer.refetch()

    case.check("split_brain_is_refused", verdict.convergence is Convergence.REFUSED, verdict)
    case.check(
        "refusal_names_sequence_conflict",
        str(verdict.reason) == "sequence_conflict",
        verdict.reason,
    )
    case.check(
        "held_state_was_not_overwritten_by_the_twin",
        consumer.held is not None and held is not None and consumer.held.digest == held.digest,
    )
    case.observations = {"verdict": verdict, "twin_revision": twin.current.revision}


def serverless_cold_start(case: Case) -> None:
    """A cold start is a new epoch with a reset counter, and that is fine.

    The trap is a consumer that treats sequence 0 after sequence 40 as a
    rollback. It is not: the epoch changed, so the counter is allowed —
    required, in fact — to start over.
    """
    clock = FakeClock()
    warm = Replica("epoch-warm", clock)
    gateway = Gateway(warm)
    consumer = _consumer(gateway, clock)
    consumer.refetch()
    for _ in range(5):
        clock.advance(1.0)
        warm.beat()
    consumer.refetch()
    before = consumer.held.sequence if consumer.held else None
    case.check("warm_instance_advanced", (before or 0) > 0, before)

    # The instance is reaped and a new one cold-starts.
    cold = Replica("epoch-cold", clock)
    gateway.pinned = cold
    clock.advance(1.0)
    verdict = consumer.refetch()

    case.check("cold_start_admitted", verdict.convergence is Convergence.ADVANCED, verdict)
    held = consumer.held
    case.check("sequence_reset_is_not_a_rollback", held is not None and held.sequence == 0, held.sequence if held else None)
    case.check("epoch_changed", held is not None and held.boot_id == "epoch-cold")
    case.observations = {"before_sequence": before, "after_sequence": held.sequence if held else None}


def rolling_deployment(case: Case) -> None:
    """Old and new instances overlap; the consumer lands on one of them.

    During a rolling deploy the gateway genuinely serves both versions
    for a window. Whichever the consumer ends on, it must be a single
    coherent epoch — never a blend — and the other must be retired.
    """
    clock = FakeClock()
    old, new = Replica("epoch-v1", clock), Replica("epoch-v2", clock)
    gateway = Gateway(old, new)
    consumer = _consumer(gateway, clock)

    verdicts = []
    for _ in range(6):
        clock.advance(1.0)
        old.beat()
        new.beat()
        verdicts.append(consumer.refetch().convergence.value)

    held = consumer.held
    case.check("ends_on_exactly_one_epoch", held is not None, held)
    case.check(
        "held_epoch_is_one_of_the_two",
        held is not None and held.boot_id in ("epoch-v1", "epoch-v2"),
        held.boot_id if held else None,
    )
    case.check(
        "the_other_epoch_is_refused_not_blended",
        any(v == "refused" for v in verdicts),
        verdicts,
    )
    case.check("never_unreachable_during_the_deploy", "unreachable" not in verdicts, verdicts)
    case.observations = {"verdicts": verdicts, "held_epoch": held.boot_id if held else None}


def asymmetric_connectivity(case: Case) -> None:
    """Hints arrive but fetches fail: the consumer must not claim freshness.

    One-way connectivity is the case where a hint channel survives and
    the authoritative path does not. A consumer that treated the hint as
    evidence would report a lease it never read.
    """
    clock = FakeClock()
    replica = Replica("epoch-a", clock)
    gateway = Gateway(replica)
    consumer = _consumer(gateway, clock)
    consumer.refetch()
    held_before = consumer.held

    gateway.unreachable = True
    for sequence in range(1, 6):
        clock.advance(1.0)
        replica.beat()
        consumer.on_hint(
            ChangeHint(
                address=f"heartbeat://participants/{PARTICIPANT}",
                revision=f"epoch-a:{sequence}",
                digest="sha256:" + "aa" * 32,
            )
        )
    verdict = consumer.refetch()

    case.check("fetch_failure_is_unreachable", verdict.convergence is Convergence.UNREACHABLE, verdict)
    case.check(
        "hints_did_not_advance_held_state",
        consumer.held is not None
        and held_before is not None
        and consumer.held.revision == held_before.revision,
        {"before": held_before.revision if held_before else None,
         "after": consumer.held.revision if consumer.held else None},
    )
    # The held lease decides on its own clock, not on the hint traffic.
    clock.advance(LEASE_SECONDS + 1.0)
    case.check("held_lease_expires_on_its_own_clock", not consumer.is_fresh())
    case.observations = {"hints_seen": consumer.hints_seen, "verdict": verdict}


def process_suspension(case: Case) -> None:
    """A suspended consumer wakes up honest.

    Mobile sleep and SIGSTOP look the same from here: the consumer stops
    evaluating for longer than the lease. On resume its very first
    answer must be "not fresh", before any refetch — otherwise a phone
    coming out of standby reports stale peers as live.
    """
    clock = FakeClock()
    replica = Replica("epoch-a", clock)
    gateway = Gateway(replica)
    consumer = _consumer(gateway, clock)
    consumer.refetch()
    case.check("fresh_before_suspension", consumer.is_fresh())

    # Suspended: the world moves, this process does not.
    clock.advance(LEASE_SECONDS * 3)

    case.check("not_fresh_on_wake_before_any_refetch", not consumer.is_fresh())
    case.check("a_refetch_is_immediately_due", consumer.due())

    # The server kept running, so a refetch recovers.
    replica.beat()
    verdict = consumer.refetch()
    case.check("recovers_by_refetch", verdict.convergence is Convergence.ADVANCED, verdict)
    case.check("fresh_again", consumer.is_fresh())
    case.observations = {"slept_seconds": LEASE_SECONDS * 3, "verdict": verdict}


def backpressure_coalesces(case: Case) -> None:
    """A burst of hints costs one refetch, not one refetch per hint.

    Presence is a level, not an event log. If fetch count scaled with
    hint volume, a chatty fleet would DoS its own consumers, so
    coalescing is a correctness property rather than an optimisation.
    """
    clock = FakeClock()
    replica = Replica("epoch-a", clock)
    gateway = Gateway(replica)
    consumer = _consumer(gateway, clock)
    consumer.refetch()

    fetches_before = consumer.fetches
    burst = 200
    for sequence in range(1, burst + 1):
        clock.advance(0.01)
        replica.beat()
        consumer.on_hint(
            ChangeHint(
                address=f"heartbeat://participants/{PARTICIPANT}",
                revision=f"epoch-a:{sequence}",
                digest="sha256:" + "bb" * 32,
            )
        )
    verdict = consumer.refetch()
    spent = consumer.fetches - fetches_before

    case.check("burst_cost_one_fetch", spent == 1, {"burst": burst, "fetches": spent})
    case.check("coalescing_was_recorded", consumer.hints_coalesced >= burst - 1, consumer.hints_coalesced)
    case.check("converged_to_the_latest", verdict.convergence is Convergence.ADVANCED, verdict)
    case.check(
        "landed_on_the_final_sequence",
        consumer.held is not None and consumer.held.sequence == burst,
        consumer.held.sequence if consumer.held else None,
    )
    case.observations = {"burst": burst, "fetches": spent, "coalesced": consumer.hints_coalesced}


def partition_and_recovery(case: Case) -> None:
    """A partition expires the lease; healing converges without a restart.

    The two halves matter equally. Expiring proves the consumer does not
    trust a lease it cannot renew; converging on heal proves it does not
    need an out-of-band reset to start believing again.
    """
    clock = FakeClock()
    replica = Replica("epoch-a", clock)
    gateway = Gateway(replica)
    consumer = _consumer(gateway, clock)
    consumer.refetch()

    gateway.unreachable = True
    attempts = []
    for _ in range(3):
        clock.advance(LEASE_SECONDS / 2)
        attempts.append(consumer.refetch().convergence.value)

    case.check("every_attempt_was_unreachable", set(attempts) == {"unreachable"}, attempts)
    case.check("lease_expired_during_the_partition", not consumer.is_fresh())
    case.check("held_state_was_not_discarded", consumer.held is not None)

    gateway.unreachable = False
    replica.beat()
    verdict = consumer.refetch()
    case.check("converges_on_heal", verdict.convergence is Convergence.ADVANCED, verdict)
    case.check("fresh_after_heal", consumer.is_fresh())
    case.check("no_restart_was_required", consumer.state.participant_id == PARTICIPANT)
    case.observations = {"attempts": attempts, "verdict": verdict}


def reconnect_inherits_nothing(case: Case) -> None:
    """A reconnecting consumer reaches the same verdict as one that stayed.

    The resource is authoritative and the session is not, so a brand new
    consumer with no history must land exactly where the long-lived one
    did. If it did not, presence would depend on connection age.
    """
    clock = FakeClock()
    replica = Replica("epoch-a", clock)
    gateway = Gateway(replica)
    long_lived = _consumer(gateway, clock)
    long_lived.refetch()
    for _ in range(4):
        clock.advance(1.0)
        replica.beat()
        long_lived.refetch()

    reconnected = _consumer(gateway, clock)
    # Asserted *before* the first fetch: "inherits nothing" is a claim
    # about the starting state, and after a fetch the new consumer has
    # legitimately recorded the epoch it just admitted.
    case.check("starts_with_no_held_lease", reconnected.held is None)
    case.check(
        "starts_with_no_retired_epochs",
        reconnected.state.retired_epochs == frozenset(),
        sorted(reconnected.state.retired_epochs),
    )

    verdict = reconnected.refetch()

    case.check("reconnected_consumer_converged", verdict.convergence is Convergence.ADVANCED, verdict)
    case.check(
        "same_revision_as_the_long_lived_consumer",
        long_lived.held is not None
        and reconnected.held is not None
        and long_lived.held.revision == reconnected.held.revision,
        {
            "long_lived": long_lived.held.revision if long_lived.held else None,
            "reconnected": reconnected.held.revision if reconnected.held else None,
        },
    )
    case.observations = {
        "long_lived_fetches": long_lived.fetches,
        "reconnected_fetches": reconnected.fetches,
    }


CASES: tuple[tuple[str, str, Any], ...] = (
    ("replicas", "Two replicas under one name keep distinct epochs", replicas_keep_distinct_epochs),
    ("round-robin", "Round-robin replay of a retired epoch is refused", round_robin_replay_is_refused),
    ("split-brain", "Two writers at one counter is a named conflict", split_brain_is_a_conflict_not_a_merge),
    ("cold-start", "A serverless cold start resets the counter legitimately", serverless_cold_start),
    ("rolling-deploy", "A rolling deployment lands on one coherent epoch", rolling_deployment),
    ("asymmetric", "Hints without fetches never manufacture freshness", asymmetric_connectivity),
    ("suspension", "A suspended consumer wakes not-fresh", process_suspension),
    ("backpressure", "A hint burst costs exactly one refetch", backpressure_coalesces),
    ("partition", "A partition expires the lease and healing converges", partition_and_recovery),
    ("reconnect", "A reconnecting consumer inherits nothing and agrees", reconnect_inherits_nothing),
)


def run() -> MatrixReport:
    """Run the distributed-runtime matrix. Always completes."""
    report = MatrixReport(
        matrix_id="distributed-runtime",
        title="Identity and epoch behaviour across replicas, gateways, and faults",
    )
    return run_cases(report, CASES)


__all__ = ["CASES", "Gateway", "PARTICIPANT", "Replica", "run"]
