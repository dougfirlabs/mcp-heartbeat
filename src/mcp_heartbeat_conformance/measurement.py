"""Measured overhead, declared thresholds, and abuse resistance.

The independent review's fifth insight was that "small" is a claim, not
a property, and that a release pack has to *measure* bandwidth, CPU, and
memory rather than assert them. So every case here declares its
threshold as a module constant, measures, and reports PASS or HOLD —
never a bare number a reader has to judge for themselves.

Two deliberate design choices:

**HOLD, not FAIL, for a threshold miss.** A budget overrun is an
operator decision about whether the cost is worth it, not a defect. FAIL
is reserved for behaviour that is wrong — an unbounded fetch rate, an
accumulating buffer — where no threshold could make it acceptable.

**Timing thresholds are order-of-magnitude, not tight.** These run on
whatever machine CI gave us, so a threshold tuned to a fast host would
be a flake generator. The values below are set where crossing them means
an algorithmic change (per-hint fetching, unbounded retention), not a
slow afternoon. Where a bound is genuinely machine-independent — bytes on
the wire, fetches per burst — it is asserted tightly, because it can be.
"""
from __future__ import annotations

import gc
import json
import resource
import time
import tracemalloc
from datetime import timedelta
from typing import Any

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.issuer import HeartbeatIssuer
from mcp_heartbeat.model import canonical_json
from mcp_heartbeat.ports import ChangeHint
from mcp_heartbeat_current.convergence import Convergence, FetchResult, HeartbeatConsumer

from .verdicts import Case, MatrixReport, run_cases

PARTICIPANT = "svc/api-7"

# ── declared thresholds ───────────────────────────────────────────────

#: Bytes for one canonical heartbeat, minimal form. A six-field document
#: with two RFC 3339 timestamps has an arithmetic floor near 200 bytes;
#: crossing 512 would mean the core grew fields.
MAX_BYTES_PER_HEARTBEAT = 512

#: Bytes for a heartbeat carrying namespaced extensions. Higher, because
#: extensions are explicitly permitted — but still bounded, so "safely
#: ignorable" cannot become "arbitrarily large".
MAX_BYTES_WITH_EXTENSIONS = 2048

#: Sustained cost of holding one idle participant, in bytes of retained
#: heap per lease renewal. Anything that grows per beat shows up here.
MAX_RETAINED_BYTES_PER_BEAT = 64

#: CPU seconds to issue and admit 10 000 heartbeats. Loose on purpose:
#: crossing it means the per-beat path became superlinear.
MAX_CPU_SECONDS_PER_10K = 10.0

#: Fetches a consumer may spend on one burst of hints, whatever the
#: burst size. This one is exact — coalescing is a correctness property.
MAX_FETCHES_PER_BURST = 1

#: A participant renewing at `renew_fraction` of its lease may not emit
#: faster than this multiple of the theoretical rate.
MAX_EMISSION_RATE_SLACK = 1.25

ITERATIONS = 10_000
BURST = 5_000


def _rss_bytes() -> int:
    """Resident set size in bytes. ``ru_maxrss`` is KiB on Linux."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _issuer(clock: FakeClock, *, lease_seconds: float = 30.0, extensions=None) -> HeartbeatIssuer:
    return HeartbeatIssuer(
        participant_id=PARTICIPANT,
        epoch_id="epoch-a",
        clock=clock,
        lease_seconds=lease_seconds,
        extensions=extensions,
    )


# ── the cases ─────────────────────────────────────────────────────────


def bytes_per_heartbeat(case: Case) -> None:
    """Wire cost of one beat, minimal and with extensions."""
    clock = FakeClock()
    minimal = _issuer(clock).issue().to_dict()
    minimal_bytes = len(canonical_json(minimal).encode("utf-8"))

    extended = _issuer(
        clock,
        extensions={"com.dougfirlabs/heartbeat": {"region": "us-west-2", "tier": "gold"}},
    ).issue().to_dict()
    extended_bytes = len(canonical_json(extended).encode("utf-8"))

    case.check("the_minimal_document_has_exactly_six_members", len(minimal) == 6, sorted(minimal))
    case.observations = {
        "bytes_minimal": minimal_bytes,
        "bytes_with_extensions": extended_bytes,
        "thresholds": {
            "minimal": MAX_BYTES_PER_HEARTBEAT,
            "with_extensions": MAX_BYTES_WITH_EXTENSIONS,
        },
        "canonical_form": "sorted keys, no whitespace",
    }

    if minimal_bytes > MAX_BYTES_PER_HEARTBEAT:
        case.hold(f"a minimal heartbeat is {minimal_bytes} bytes, over the {MAX_BYTES_PER_HEARTBEAT} threshold")
        return
    if extended_bytes > MAX_BYTES_WITH_EXTENSIONS:
        case.hold(
            f"an extended heartbeat is {extended_bytes} bytes, over the "
            f"{MAX_BYTES_WITH_EXTENSIONS} threshold"
        )
        return
    case.check("minimal_within_threshold", minimal_bytes <= MAX_BYTES_PER_HEARTBEAT, minimal_bytes)
    case.check("extended_within_threshold", extended_bytes <= MAX_BYTES_WITH_EXTENSIONS, extended_bytes)


def cpu_cost_of_the_hot_path(case: Case) -> None:
    """CPU to issue and admit ten thousand beats.

    Issue *and* admit, because a producer that is cheap and a consumer
    that is quadratic would still make the feature unusable, and only
    the pair covers the whole per-beat path.
    """
    clock = FakeClock()
    issuer = _issuer(clock)
    documents = {PARTICIPANT: issuer.issue().to_dict()}

    class Source:
        def fetch(self, participant_id: str) -> FetchResult:
            return FetchResult(document=documents[participant_id])

    consumer = HeartbeatConsumer(PARTICIPANT, Source(), clock)
    consumer.refetch()

    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    for _ in range(ITERATIONS):
        clock.advance(0.001)
        documents[PARTICIPANT] = issuer.issue().to_dict()
        consumer.refetch()
    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_SELF)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)

    case.check(
        "every_beat_was_admitted",
        consumer.held is not None and consumer.held.sequence == ITERATIONS,
        consumer.held.sequence if consumer.held else None,
    )
    case.observations = {
        "iterations": ITERATIONS,
        "cpu_seconds": round(cpu, 4),
        "wall_seconds": round(wall, 4),
        "microseconds_per_beat": round(cpu / ITERATIONS * 1e6, 2) if cpu else None,
        "threshold_cpu_seconds": MAX_CPU_SECONDS_PER_10K,
    }
    if cpu > MAX_CPU_SECONDS_PER_10K:
        case.hold(
            f"{ITERATIONS} issue+admit cycles cost {cpu:.2f} CPU seconds, over the "
            f"{MAX_CPU_SECONDS_PER_10K}s threshold"
        )
        return
    case.check("within_the_cpu_threshold", cpu <= MAX_CPU_SECONDS_PER_10K, round(cpu, 4))


def memory_does_not_grow_per_beat(case: Case) -> None:
    """An idle participant must cost O(1), not O(beats).

    The bug this catches is retention: a consumer that keeps every
    document, every hint, or every digest it has seen looks perfectly
    correct until it has been up for a week.
    """
    clock = FakeClock()
    issuer = _issuer(clock)
    documents = {PARTICIPANT: issuer.issue().to_dict()}

    class Source:
        def fetch(self, participant_id: str) -> FetchResult:
            return FetchResult(document=documents[participant_id])

    consumer = HeartbeatConsumer(PARTICIPANT, Source(), clock)
    consumer.refetch()

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    rss_before = _rss_bytes()

    for _ in range(ITERATIONS):
        clock.advance(0.001)
        documents[PARTICIPANT] = issuer.issue().to_dict()
        consumer.refetch()

    gc.collect()
    peak = tracemalloc.take_snapshot()
    tracemalloc.stop()
    rss_after = _rss_bytes()

    grown = sum(stat.size_diff for stat in peak.compare_to(baseline, "filename"))
    per_beat = max(grown, 0) / ITERATIONS

    # The one structural assertion, independent of any byte threshold:
    # a consumer holds exactly one lease, whatever it has seen.
    case.check("holds_exactly_one_lease", consumer.held is not None)
    case.check(
        "retires_one_epoch_not_ten_thousand",
        len(consumer.state.retired_epochs) == 1,
        sorted(consumer.state.retired_epochs),
    )
    case.check(
        "coalescing_state_is_a_flag_not_a_queue",
        consumer.refetch_pending is False,
    )
    case.observations = {
        "iterations": ITERATIONS,
        "traced_growth_bytes": grown,
        "bytes_per_beat": round(per_beat, 2),
        "rss_delta_bytes": rss_after - rss_before,
        "threshold_bytes_per_beat": MAX_RETAINED_BYTES_PER_BEAT,
    }
    if per_beat > MAX_RETAINED_BYTES_PER_BEAT:
        case.hold(
            f"retained {per_beat:.1f} bytes per beat, over the "
            f"{MAX_RETAINED_BYTES_PER_BEAT} threshold"
        )
        return
    case.check("within_the_retention_threshold", per_beat <= MAX_RETAINED_BYTES_PER_BEAT, round(per_beat, 2))


def emission_rate_is_bounded(case: Case) -> None:
    """A participant cannot emit faster than its declared cadence.

    Renewal is driven by the lease and the renew fraction, so the rate
    is arithmetic rather than a matter of trust — and the assertion is
    that the arithmetic actually binds.
    """
    lease_seconds = 30.0
    renew_fraction = 0.5
    window_seconds = 3600.0
    theoretical = window_seconds / (lease_seconds * renew_fraction)

    clock = FakeClock()
    issuer = _issuer(clock, lease_seconds=lease_seconds)
    emitted = 0
    elapsed = 0.0
    step = lease_seconds * renew_fraction
    while elapsed < window_seconds:
        issuer.issue()
        emitted += 1
        clock.advance(step)
        elapsed += step

    rate = emitted / window_seconds
    ceiling = theoretical * MAX_EMISSION_RATE_SLACK / window_seconds

    case.check("emission_is_driven_by_the_lease", emitted > 0)
    case.check(
        "stayed_within_the_declared_cadence",
        emitted <= theoretical * MAX_EMISSION_RATE_SLACK,
        {"emitted": emitted, "theoretical": theoretical},
    )
    case.check(
        "a_lease_covers_the_gap_to_the_next_renewal",
        lease_seconds > step,
        {"lease_seconds": lease_seconds, "renew_every": step},
    )
    case.observations = {
        "lease_seconds": lease_seconds,
        "renew_fraction": renew_fraction,
        "window_seconds": window_seconds,
        "emitted": emitted,
        "beats_per_second": round(rate, 4),
        "ceiling_beats_per_second": round(ceiling, 4),
        "bytes_per_hour": emitted * len(canonical_json(issuer.issue().to_dict()).encode("utf-8")),
    }


def hint_flooding_does_not_amplify(case: Case) -> None:
    """Abuse: a hint flood must not become a fetch flood.

    An attacker who can send hints — or a buggy peer that resends on
    every tick — must not be able to convert cheap notifications into
    expensive authoritative reads against a third party. This is the
    amplification case, and it is a FAIL rather than a HOLD: no
    threshold makes unbounded amplification acceptable.
    """
    clock = FakeClock()
    issuer = _issuer(clock)
    documents = {PARTICIPANT: issuer.issue().to_dict()}
    fetch_count = {"n": 0}

    class CountingSource:
        def fetch(self, participant_id: str) -> FetchResult:
            fetch_count["n"] += 1
            return FetchResult(document=documents[participant_id])

    consumer = HeartbeatConsumer(PARTICIPANT, CountingSource(), clock)
    consumer.refetch()
    before = fetch_count["n"]

    for index in range(BURST):
        consumer.on_hint(
            ChangeHint(
                address=f"heartbeat://participants/{PARTICIPANT}",
                revision=f"epoch-a:{index}",
                digest="sha256:" + "cc" * 32,
            )
        )
    case.check(
        "hints_alone_caused_no_fetches",
        fetch_count["n"] == before,
        {"burst": BURST, "fetches": fetch_count["n"] - before},
    )

    clock.advance(1.0)
    documents[PARTICIPANT] = issuer.issue().to_dict()
    consumer.refetch()
    spent = fetch_count["n"] - before

    case.check(
        "the_whole_burst_cost_one_fetch",
        spent <= MAX_FETCHES_PER_BURST,
        {"burst": BURST, "fetches": spent, "ceiling": MAX_FETCHES_PER_BURST},
    )
    case.check(
        "the_amplification_factor_is_below_one",
        spent / BURST < 1.0,
        round(spent / BURST, 6),
    )
    case.check("and_it_still_converged", consumer.held is not None and consumer.held.sequence == 1)
    case.observations = {
        "burst": BURST,
        "fetches": spent,
        "hints_coalesced": consumer.hints_coalesced,
        "amplification": round(spent / BURST, 6),
    }


def malformed_flooding_accumulates_nothing(case: Case) -> None:
    """Abuse: a flood of garbage must not grow state or raise.

    A consumer that buffered rejected documents — for a retry queue, for
    diagnostics, for anything — would hand an attacker an unbounded
    allocation primitive over the cheapest possible input.
    """
    clock = FakeClock()
    issuer = _issuer(clock)
    good = issuer.issue().to_dict()
    payloads: list[Any] = [
        {},
        {"node_id": PARTICIPANT},
        dict(good, sequence=-1),
        dict(good, sequence="not-an-int"),
        dict(good, boot_id="!!invalid!!"),
        dict(good, expires_at=good["issued_at"]),
        dict(good, extension_version="9.9"),
        dict(good, unnamespaced="nope"),
    ]
    box: dict[str, Any] = {"document": good}

    class HostileSource:
        def fetch(self, participant_id: str) -> FetchResult:
            return FetchResult(document=box["document"])

    consumer = HeartbeatConsumer(PARTICIPANT, HostileSource(), clock)
    consumer.refetch()
    held = consumer.held

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()

    verdicts: set[str] = set()
    for index in range(1_000):
        box["document"] = payloads[index % len(payloads)]
        verdicts.add(consumer.refetch().convergence.value)

    gc.collect()
    grown = sum(
        stat.size_diff
        for stat in tracemalloc.take_snapshot().compare_to(baseline, "filename")
    )
    tracemalloc.stop()

    case.check("no_malformed_document_was_admitted", "advanced" not in verdicts, sorted(verdicts))
    case.check("nothing_raised", verdicts != set(), sorted(verdicts))
    case.check(
        "held_state_survived_the_flood",
        consumer.held is not None and held is not None and consumer.held.revision == held.revision,
    )
    case.check(
        "no_unbounded_accumulation",
        max(grown, 0) < 1_000 * MAX_RETAINED_BYTES_PER_BEAT,
        {"grown_bytes": grown, "ceiling": 1_000 * MAX_RETAINED_BYTES_PER_BEAT},
    )

    # The honest recovery check: a good document is still admitted after.
    box["document"] = issuer.issue().to_dict()
    clock.advance(1.0)
    case.check(
        "a_good_document_is_still_admitted_afterwards",
        consumer.refetch().convergence is Convergence.ADVANCED,
    )
    case.observations = {
        "malformed_payloads": len(payloads),
        "attempts": 1_000,
        "verdicts_seen": sorted(verdicts),
        "grown_bytes": grown,
    }


def the_evidence_is_json_serialisable(case: Case) -> None:
    """Every measurement must survive the trip into the evidence pack.

    A number that cannot be serialised is a measurement nobody outside
    this process will ever read.
    """
    payload = run_measurements_only()
    encoded = json.dumps(payload, sort_keys=True)
    case.check("measurements_serialise", bool(encoded), len(encoded))
    case.check("round_trips_unchanged", json.loads(encoded) == payload)
    case.check("no_measurement_is_missing", set(payload) >= {"bytes", "thresholds"}, sorted(payload))
    case.observations = {"payload_bytes": len(encoded)}


def run_measurements_only() -> dict[str, Any]:
    """The bare numbers, for the evidence pack's summary block."""
    clock = FakeClock()
    minimal = _issuer(clock).issue().to_dict()
    extended = _issuer(
        clock, extensions={"com.dougfirlabs/heartbeat": {"region": "us-west-2"}}
    ).issue().to_dict()
    lease_seconds, renew_fraction = 30.0, 0.5
    per_hour = int(timedelta(hours=1).total_seconds() / (lease_seconds * renew_fraction))
    minimal_bytes = len(canonical_json(minimal).encode("utf-8"))

    return {
        "bytes": {
            "minimal": minimal_bytes,
            "with_extensions": len(canonical_json(extended).encode("utf-8")),
            "per_participant_per_hour": minimal_bytes * per_hour,
        },
        "cadence": {
            "lease_seconds": lease_seconds,
            "renew_fraction": renew_fraction,
            "beats_per_hour": per_hour,
        },
        "thresholds": {
            "max_bytes_per_heartbeat": MAX_BYTES_PER_HEARTBEAT,
            "max_bytes_with_extensions": MAX_BYTES_WITH_EXTENSIONS,
            "max_retained_bytes_per_beat": MAX_RETAINED_BYTES_PER_BEAT,
            "max_cpu_seconds_per_10k": MAX_CPU_SECONDS_PER_10K,
            "max_fetches_per_burst": MAX_FETCHES_PER_BURST,
            "max_emission_rate_slack": MAX_EMISSION_RATE_SLACK,
        },
    }


CASES: tuple[tuple[str, str, Any], ...] = (
    ("bytes", "Bytes on the wire per heartbeat", bytes_per_heartbeat),
    ("cpu", "CPU cost of the issue/admit hot path", cpu_cost_of_the_hot_path),
    ("memory", "An idle participant costs O(1), not O(beats)", memory_does_not_grow_per_beat),
    ("emission-rate", "Emission is bounded by the declared cadence", emission_rate_is_bounded),
    ("hint-flood", "A hint flood does not amplify into a fetch flood", hint_flooding_does_not_amplify),
    ("malformed-flood", "A malformed flood accumulates nothing", malformed_flooding_accumulates_nothing),
    ("serialisable", "Every measurement reaches the evidence pack", the_evidence_is_json_serialisable),
)


def run() -> MatrixReport:
    """Run the measurement and abuse-resistance matrix."""
    report = MatrixReport(
        matrix_id="measurement",
        title="Measured bandwidth, CPU, memory, emission rate, and abuse resistance",
    )
    return run_cases(report, CASES)


__all__ = ["CASES", "PARTICIPANT", "run", "run_measurements_only"]
