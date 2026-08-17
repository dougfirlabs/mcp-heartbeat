"""The observing participant: holds lineage state and renders the verdict.

The other half of the demonstration. It polls the publisher's `/heartbeat`,
feeds every document to :func:`mcp_heartbeat.admit`, and keeps the resulting
:class:`~mcp_heartbeat.LineageState`. Every judgement it makes comes from that
held state and from the shipped core — the observer implements no admission
logic of its own, because a demonstration whose oracle re-implements the thing
under test proves only that two copies of an idea agree.

Five scenarios, run in order against one live publisher:

``handshake``
    A first heartbeat is admitted, opening an epoch at ``sequence`` 0.

``renewal``
    Later heartbeats are admitted, the sequence rises strictly, and the held
    lease's expiry moves forward.

``status-transition``
    The publisher flips its advertised status ``serving`` → ``draining``. The
    observer sees the change on a **refetched** document, and asserts the lease
    judgement is unaffected — status is advisory metadata in a namespaced
    extension, not an input to admission. *Freshness is not permission*, and
    neither is status.

``expiry``
    The publisher is frozen: still reachable, still answering, no longer
    minting. Three separate things then have to hold, and they are not the
    same claim. The observer's **held** lease lapses with no notification
    arriving — that is what a consumer must do on its own. The redelivered
    document is recognised as an idempotent **duplicate**, not an error,
    because ``admit()`` tests duplication before freshness and a frozen
    participant re-serves exactly what it last minted. And a **fresh**
    consumer, which has no duplicate to recognise, refuses those same bytes
    as ``expired_on_arrival`` — so a lapsed lease cannot be inherited.

``recovery``
    The publisher restarts under a new epoch. The observer admits the new
    stream, sees ``sequence`` reset and ``boot_id`` change, and confirms the
    retired epoch can never come back: replaying it is refused as
    ``boot_id_reuse``.

Standard library only, and no third-party HTTP client.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from mcp_heartbeat import LineageState, ViolationCode, admit

LAYOUT = "standalone"

STATUS_EXTENSION = "com.dougfirlabs/heartbeat"

#: The observer widens the skew bound well past the lease on purpose.
#:
#: ``check_freshness`` tests skew *before* expiry, so with the default 5s bound
#: a frozen participant's document would be refused as ``clock_skew_exceeded``
#: — a true statement about a different thing. The scenario is about a lease
#: lapsing, so the bound is raised until expiry is the reason that fires.
#: Narrowing it back would not make the demonstration stricter, only vaguer.
MAX_SKEW_SECONDS = 3600.0


class Timeout(Exception):
    """A condition never became true inside its budget."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Publisher:
    """A thin client for the publishing participant's three routes."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def heartbeat(self) -> dict[str, Any]:
        return self._request("/heartbeat")

    def healthz(self) -> dict[str, Any]:
        return self._request("/healthz")

    def state(self) -> dict[str, Any]:
        return self._request("/state")

    def control(self, op: str, value: Any = None) -> dict[str, Any]:
        return self._request("/control", {"op": op, "value": value})

    def wait_ready(self, *, budget_seconds: float = 60.0) -> None:
        deadline = time.monotonic() + budget_seconds
        while time.monotonic() < deadline:
            try:
                self.healthz()
                return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        raise Timeout(f"publisher never became reachable at {self.base_url}")


class Observation:
    """One scenario's result: a verdict, its checks, and what was seen."""

    def __init__(self, scenario_id: str, title: str) -> None:
        self.scenario_id = scenario_id
        self.title = title
        self.checks: list[dict[str, Any]] = []
        self.observations: dict[str, Any] = {}

    def check(self, name: str, passed: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})

    @property
    def verdict(self) -> str:
        return "PASS" if all(entry["passed"] for entry in self.checks) else "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "verdict": self.verdict,
            "checks": self.checks,
            "observations": self.observations,
        }


class Session:
    """Holds the lineage state across every scenario, as a consumer would."""

    def __init__(self, publisher: Publisher, participant_id: str) -> None:
        self.publisher = publisher
        self.state = LineageState(participant_id=participant_id)
        self.transitions: list[dict[str, Any]] = []
        self.refusals: list[dict[str, Any]] = []

    def poll(self) -> tuple[dict[str, Any], Any]:
        """Refetch, admit, and record. Returns the document and the outcome."""
        document = self.publisher.heartbeat()
        outcome = admit(self.state, document, _now(), max_skew_seconds=MAX_SKEW_SECONDS)
        if outcome.accepted:
            self.state = outcome.state
            self.transitions.append(
                {
                    "at": _stamp(),
                    "epoch_id": document["boot_id"],
                    "sequence": document["sequence"],
                    "expires_at": document["expires_at"],
                }
            )
        elif outcome.reason is not None:
            self.refusals.append(
                {"at": _stamp(), "reason": outcome.reason.value, "sequence": document.get("sequence")}
            )
        return document, outcome

    def held_remaining(self) -> float:
        """Seconds left on the lease this observer currently holds."""
        return self.state.held.remaining_seconds(_now()) if self.state.held else -1.0

    def wait_until(
        self, predicate: Callable[[], bool], *, budget_seconds: float, interval: float = 0.25
    ) -> float:
        """Poll ``predicate`` until true; return how long it took."""
        started = time.monotonic()
        deadline = started + budget_seconds
        while time.monotonic() < deadline:
            if predicate():
                return round(time.monotonic() - started, 3)
            time.sleep(interval)
        raise Timeout("condition never became true within its budget")


# ── the scenarios ─────────────────────────────────────────────────────


def scenario_handshake(session: Session) -> Observation:
    result = Observation("handshake", "A first heartbeat opens an epoch at sequence 0")
    document, outcome = session.poll()
    result.check("the_first_heartbeat_is_admitted", outcome.accepted, getattr(outcome.reason, "value", None))
    result.check("it_opens_the_stream_at_sequence_zero", document["sequence"] == 0, document["sequence"])
    result.check(
        "the_observer_now_holds_a_fresh_lease",
        session.held_remaining() > 0,
        session.held_remaining(),
    )
    # The point of the whole contract, stated as an assertion rather than a
    # comment: the liveness endpoint is not the authority.
    health = session.publisher.healthz()
    result.check(
        "healthz_reports_process_liveness_only_and_says_so",
        "does_not_mean" in health,
        health,
    )
    result.observations = {"epoch_id": document["boot_id"], "healthz": health}
    return result


def scenario_renewal(session: Session) -> Observation:
    result = Observation("renewal", "Later heartbeats are admitted and the sequence rises")
    first = session.state.held
    assert first is not None
    sequences = [first.sequence]
    expiries = [first.expires_at]
    for _ in range(3):
        time.sleep(0.4)
        document, outcome = session.poll()
        result.check(
            f"heartbeat_{document['sequence']}_is_admitted",
            outcome.accepted,
            getattr(outcome.reason, "value", None),
        )
        held = session.state.held
        assert held is not None
        sequences.append(held.sequence)
        expiries.append(held.expires_at)

    result.check(
        "the_sequence_rises_strictly",
        all(b > a for a, b in zip(sequences, sequences[1:])),
        sequences,
    )
    result.check(
        "and_the_held_lease_moves_forward",
        all(b > a for a, b in zip(expiries, expiries[1:])),
        [e.isoformat() for e in expiries],
    )
    result.check("the_epoch_did_not_change", len(set(t["epoch_id"] for t in session.transitions)) == 1)
    result.observations = {"sequences": sequences}
    return result


def scenario_status_transition(session: Session) -> Observation:
    result = Observation(
        "status-transition", "A status change is visible on refetch and does not affect the lease"
    )
    before_document, _ = session.poll()
    before = before_document.get("extensions", {}).get(STATUS_EXTENSION, {}).get("status")
    result.check("the_publisher_starts_out_serving", before == "serving", before)

    session.publisher.control("status", "draining")

    # Refetched, never inferred from the control response. The control channel
    # is scaffolding; the authoritative answer is the document itself.
    after_document, outcome = session.poll()
    after = after_document.get("extensions", {}).get(STATUS_EXTENSION, {}).get("status")
    result.check("the_transition_is_visible_on_a_refetched_document", after == "draining", after)
    result.check(
        "the_heartbeat_carrying_it_is_still_admitted",
        outcome.accepted,
        getattr(outcome.reason, "value", None),
    )
    result.check(
        "the_lease_is_unaffected_by_the_status_change",
        session.held_remaining() > 0,
        session.held_remaining(),
    )
    # Draining is not a fault, and a draining participant is not an expired
    # one. Collapsing the two is the mistake this scenario exists to rule out.
    result.check(
        "a_draining_participant_is_still_fresh",
        session.state.held is not None and session.state.held.is_fresh(_now()),
    )
    result.check(
        "the_status_rides_in_a_namespaced_extension",
        "." in STATUS_EXTENSION and STATUS_EXTENSION in after_document.get("extensions", {}),
        sorted(after_document.get("extensions", {})),
    )
    result.observations = {"status_before": before, "status_after": after}
    return result


def scenario_expiry(session: Session) -> Observation:
    result = Observation("expiry", "A reachable but frozen participant's lease still lapses")
    lease = session.state.held
    assert lease is not None
    budget = lease.remaining_seconds(_now()) + 10.0

    session.publisher.control("freeze")
    frozen_at = _stamp()

    # It is still answering. That is the trap, so assert it rather than
    # assume it: a consumer that equates a 200 with liveness fails here.
    health = session.publisher.healthz()
    result.check("the_frozen_participant_is_still_reachable", health.get("process") == "alive", health)

    # The observer notices from its OWN held state. No notification arrives,
    # and none is needed — that is the difference between a lease and a ping.
    elapsed = session.wait_until(lambda: session.held_remaining() <= 0, budget_seconds=budget)
    result.check("the_held_lease_lapses_without_any_notification", session.held_remaining() <= 0, elapsed)

    # What arrives now is the held document, byte for byte — a frozen
    # participant re-serves what it last minted. `admit()` tests duplication
    # *before* freshness, so the honest answer is `duplicate`, not a
    # violation: redelivery is idempotent and carries no reason code. It is
    # neither a transition nor an error, and asserting `expired_on_arrival`
    # here would be demanding the wrong verdict for the right situation.
    stale_document, outcome = session.poll()
    result.check("the_redelivered_document_is_not_admitted_as_new", not outcome.accepted)
    result.check(
        "it_is_recognised_as_an_idempotent_duplicate_not_an_error",
        outcome.duplicate and outcome.reason is None,
        {"duplicate": outcome.duplicate, "reason": getattr(outcome.reason, "value", None)},
    )

    # Expiry-on-arrival is a statement about a consumer that does *not*
    # already hold the document. A newcomer offered the same stale bytes has
    # no duplicate to recognise, so freshness is the check that fires — and
    # it fires as expiry rather than skew, which is what MAX_SKEW_SECONDS
    # above is widened to make observable.
    newcomer = LineageState(participant_id=session.state.participant_id)
    newcomer_outcome = admit(newcomer, stale_document, _now(), max_skew_seconds=MAX_SKEW_SECONDS)
    result.check(
        "a_fresh_consumer_refuses_the_same_document_as_expired_on_arrival",
        newcomer_outcome.reason == ViolationCode.EXPIRED_ON_ARRIVAL,
        getattr(newcomer_outcome.reason, "value", None),
    )
    result.check(
        "so_a_lapsed_lease_cannot_be_inherited_by_a_new_observer",
        newcomer_outcome.state.held is None,
    )

    # Fail closed: neither outcome may disturb what is held.
    held_after = session.state.held
    result.check(
        "and_the_held_state_is_untouched_throughout",
        held_after is not None and held_after.digest == lease.digest,
    )
    result.observations = {
        "frozen_at": frozen_at,
        "seconds_until_lapse": elapsed,
        "lease_seconds": round((lease.expires_at - lease.issued_at).total_seconds(), 3),
    }
    return result


def scenario_recovery(session: Session) -> Observation:
    result = Observation("recovery", "A restart opens a new epoch and the old one can never return")
    retired = session.state.held
    assert retired is not None
    retired_document = retired.to_dict()

    session.publisher.control("restart")
    session.wait_until(
        lambda: session.poll()[1].accepted, budget_seconds=30.0, interval=0.5
    )

    recovered = session.state.held
    assert recovered is not None
    result.check("the_participant_is_admitted_again", recovered.digest != retired.digest)
    result.check("under_a_new_epoch", recovered.boot_id != retired.boot_id, recovered.boot_id)
    result.check("with_the_sequence_reset", recovered.sequence == 0, recovered.sequence)
    result.check("and_a_fresh_lease", recovered.is_fresh(_now()), session.held_remaining())

    # The replay defence. A retired epoch reappearing is not a stale message,
    # it is an attack shape, and it has its own reason code.
    replay = admit(session.state, retired_document, _now(), max_skew_seconds=MAX_SKEW_SECONDS)
    result.check(
        "replaying_the_retired_epoch_is_refused_as_boot_id_reuse",
        replay.reason == ViolationCode.BOOT_ID_REUSE,
        getattr(replay.reason, "value", None),
    )
    result.check(
        "and_that_replay_did_not_disturb_the_held_state",
        session.state.held is not None and session.state.held.digest == recovered.digest,
    )
    result.observations = {
        "retired_epoch": retired.boot_id,
        "recovered_epoch": recovered.boot_id,
        "retired_epochs_tracked": len(session.state.retired_epochs),
    }
    return result


SCENARIOS = (
    scenario_handshake,
    scenario_renewal,
    scenario_status_transition,
    scenario_expiry,
    scenario_recovery,
)


def run(*, base_url: str, participant_id: str) -> dict[str, Any]:
    """Run every scenario in order against one live publisher."""
    started = _stamp()
    started_monotonic = time.monotonic()

    publisher = Publisher(base_url)
    publisher.wait_ready()
    session = Session(publisher, participant_id)

    results: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        began = time.monotonic()
        try:
            observation = scenario(session)
        except Timeout as exc:
            observation = Observation(scenario.__name__, "timed out")
            observation.check("the_scenario_completed_within_its_budget", False, str(exc))
        payload = observation.to_dict()
        payload["duration_seconds"] = round(time.monotonic() - began, 3)
        results.append(payload)
        print(f"[{payload['verdict']}] {payload['scenario_id']}: {payload['title']}", flush=True)

    failed = [entry["scenario_id"] for entry in results if entry["verdict"] == "FAIL"]
    return {
        "schema_version": "0.1",
        "run": {
            "participant_id": participant_id,
            "base_url": base_url,
            "started_at": started,
            "finished_at": _stamp(),
            "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        },
        "environment": {
            "layout": LAYOUT,
            "max_skew_seconds": MAX_SKEW_SECONDS,
            # Read, never set. The observer's last act is not to change the
            # subject it just finished judging.
            "publisher_state": publisher.state(),
        },
        "scenarios": results,
        "ledgers": {"transitions": session.transitions, "refusals": session.refusals},
        "summary": {
            "total": len(results),
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "ok": not failed,
            "failed_scenarios": failed,
        },
    }


def main() -> int:
    base_url = os.environ.get("HB_DEMO_PUBLISHER_URL", "http://127.0.0.1:8981")
    participant_id = os.environ.get("HB_DEMO_PARTICIPANT_ID", "demo/publisher-1")
    output = os.environ.get("HB_DEMO_EVIDENCE", "/evidence/observation.json")

    record = run(base_url=base_url, participant_id=participant_id)

    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(payload)

    summary = record["summary"]
    print(
        f"\n{summary['passed']}/{summary['total']} scenarios passed — observation at {output}",
        flush=True,
    )
    if summary["failed_scenarios"]:
        print(f"failed: {', '.join(summary['failed_scenarios'])}", flush=True)
    return 0 if summary["ok"] else 1


__all__ = ["LAYOUT", "MAX_SKEW_SECONDS", "Observation", "Publisher", "Session", "main", "run"]
