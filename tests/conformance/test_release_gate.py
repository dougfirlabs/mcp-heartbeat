"""The release gate, and the three items it used to be holding on.

:mod:`test_matrices` asserts the properties that must hold whatever the
verdicts are. This module does the opposite: it pins the *specific*
state of the world today, so that reopening one of these items — or
letting a new one open — is a deliberate act with a failing test
attached, rather than a number quietly changing in a JSON file.

All three are now closed, and each is asserted below in its closed state
together with what did *not* change alongside it. That pairing is the
discipline: every one of these could have been made to clear by moving
the thing it was measured against, and the second half of each test is
what says it was not.

1. **The official SDK v2 leg.** Closed by **HB-X3**: the leg was run,
   in the isolated venv ``tools/verify_sdk.sh`` builds, against the real
   ``mcp==2.0.0`` — 171 passed — and that run wrote
   ``docs/sdk-verification.json``. The criterion did not move and the
   verdict vocabulary did not gain a softer rung; what changed is that
   the run happened. The record is keyed to a digest of the sources it
   exercised, so it lapses on any edit to them.
2. **The portable core is 430 logical LOC against a 400 budget.** Closed
   by **D1**: the overrun is signed off in
   ``docs/core-size-signoff.json``. The measurement did not move, and
   the tests below assert that it did not — a signoff that came with a
   shrinking core would be a different and much less honest event.
3. **The two eras answered a policy gap differently.** Closed by **D2**:
   both eras now fail closed to ``unbound``. Neither ever granted
   authority; what changed is that the observable answers agree.

If you uninstall the SDK, edit the attested tree, change the core's size,
or reopen either decision, the corresponding test fails and tells you to
update this file — which is the point.
"""
from __future__ import annotations

import json

import pytest

from mcp_heartbeat_conformance import release
from mcp_heartbeat_conformance.release import LEVELS, RELEASE_GATES, adjudicate
from mcp_heartbeat_conformance.verdicts import Verdict


@pytest.fixture(scope="module")
def matrices():
    import mcp_heartbeat_conformance as hb5

    return hb5.run_matrices()


@pytest.fixture(scope="module")
def report(matrices):
    return adjudicate(matrices)


# ── the three levels ──────────────────────────────────────────────────


def test_the_three_levels_are_declared_with_distinct_criteria() -> None:
    keys = [level.key for level in LEVELS]
    assert keys == ["ready_to_dogfood", "ready_for_external_review", "ready_to_publish"]

    criteria = {level.key: {c.key for c in level.criteria} for level in LEVELS}
    assert len({frozenset(v) for v in criteria.values()}) == 3, "two levels share one criteria set"
    # Publish must ask something review does not, or it is review under
    # another name — the PRD's "none is inferred from another".
    assert criteria["ready_to_publish"] - criteria["ready_for_external_review"]


def test_every_level_states_what_it_means_in_prose(report) -> None:
    """A verdict a reader cannot interpret is not a verdict."""
    for level in report.levels:
        assert level["means"] and len(level["means"]) > 40, level["key"]
        assert level["criteria"], level["key"]


def test_dogfood_and_external_review_are_ready(report) -> None:
    by_key = {level["key"]: level for level in report.levels}
    assert by_key["ready_to_dogfood"]["ready"] is True, by_key["ready_to_dogfood"]["unmet"]
    assert by_key["ready_for_external_review"]["ready"] is True, by_key["ready_for_external_review"]["unmet"]


def test_publish_is_ready_with_every_criterion_met(report) -> None:
    """The last two unmet criteria closed, and none of them by relaxation.

    Both named the same case, so both had to be closed the same way: by
    running it. ``every_declared_leg_was_exercised`` counts UNSUPPORTED
    verdicts and ``official_sdk_leg_proven`` demands a PASS on
    ``current-current-sdk``, and neither predicate was touched — the
    seven criteria are still the seven, asserted here by name so a future
    change that clears the level by dropping one fails instead.
    """
    by_key = {level["key"]: level for level in report.levels}
    publish = by_key["ready_to_publish"]

    assert publish["ready"] is True, json.dumps(publish["unmet"], indent=2)
    assert publish["unmet"] == []
    assert {c["key"] for c in publish["criteria"]} == {
        "no_failures_anywhere",
        "no_holds_anywhere",
        "every_declared_leg_was_exercised",
        "official_sdk_leg_proven",
        "core_within_prd_budget",
        "identity_semantics_settled",
        "downgrade_confusion_closed",
    }
    assert all(c["met"] for c in publish["criteria"])


# ── the three closed items, by name ───────────────────────────────────


def test_the_official_sdk_leg_was_run_not_reclassified(matrices) -> None:
    """The leg clears because a real run of it is on record.

    The distinction this test exists to hold: the case does not pass
    because an absent SDK stopped counting against it, but because
    ``tools/verify_sdk.sh`` ran ``tests/current`` against the pinned
    distribution and the counts of that run are here to read. A record
    with a failure in it, or one describing a different tree, would not
    clear it — :func:`test_an_attestation_does_not_transfer_to_another_tree`
    is that half.
    """
    case = next(c for c in matrices["cross-era"].cases if c.case_id == "current-current-sdk")
    assert case.verdict is Verdict.PASS, case.reason

    attested = case.observations["attestation"]
    assert attested["sdk_distribution"] == "mcp"
    assert attested["sdk_version"] == "2.0.0"
    assert attested["sdk_types_version"] == "2.0.0"
    assert attested["matches_pin"] is True

    # A run with results, not an empty collection reporting no failures.
    assert attested["tests_failed"] == 0
    assert attested["tests_passed"] > 0
    assert attested["tests_collected"] == attested["tests_passed"] + attested["tests_skipped"]

    # The contract was re-derived from the installed SDK during that run,
    # which is what makes the pure layer a checked copy rather than a
    # transcription that has since drifted.
    assert attested["constants_rederived"] > 0
    assert attested["implements_revision"] == "2026-07-28"

    # And the adapter-level leg passes independently, so the two legs
    # still corroborate rather than share a single source of truth.
    adapter = next(c for c in matrices["cross-era"].cases if c.case_id == "current-current")
    assert adapter.verdict is Verdict.PASS


def test_an_attestation_does_not_transfer_to_another_tree() -> None:
    """The anti-gaming property for the SDK leg, stated directly.

    Same shape as :func:`test_a_signoff_does_not_transfer_to_a_different_measurement`:
    a record of one run must say nothing about a different tree, a
    different SDK, or a run that failed. Otherwise "the leg was
    exercised" decays into "the leg was exercised once, against
    something", which is the claim the PRD's first hard constraint
    forbids.
    """
    from mcp_heartbeat_conformance import sdk_attestation

    record = sdk_attestation.load_attestation()
    assert record is not None, "the SDK verification record is missing"

    digest = sdk_attestation.adapter_digest()
    assert sdk_attestation.attestation_covers(record, digest) is True

    # A different tree — the case an edit to the adapter produces.
    assert sdk_attestation.attestation_covers(record, "0" * 64) is False
    assert sdk_attestation.attestation_covers({**record, "adapter_digest": "0" * 64}, digest) is False
    # A different SDK.
    assert sdk_attestation.attestation_covers({**record, "sdk_version": "1.9.0"}, digest) is False
    assert sdk_attestation.attestation_covers({**record, "sdk_types_version": "1.9.0"}, digest) is False
    assert sdk_attestation.attestation_covers({**record, "implements_revision": "2025-06-18"}, digest) is False
    # A run that did not pass, and a run that collected nothing.
    assert sdk_attestation.attestation_covers({**record, "tests_failed": 1}, digest) is False
    assert sdk_attestation.attestation_covers({**record, "tests_passed": 0}, digest) is False
    # A file that is not this kind of record, and one missing provenance.
    assert sdk_attestation.attestation_covers({**record, "record": "something-else"}, digest) is False
    assert sdk_attestation.attestation_covers({**record, "recorded_by": ""}, digest) is False
    assert sdk_attestation.attestation_covers(None, digest) is False


def test_the_attested_digest_covers_the_sources_the_run_exercised() -> None:
    """The digest must name the adapter, not merely something.

    A digest computed over an empty or unrelated file set would still be
    stable and still compare equal — and would pin nothing. This asserts
    it covers the core, the current adapter, and the suite that ran,
    and that the conformance package is deliberately outside it.
    """
    from mcp_heartbeat_conformance import sdk_attestation

    covered = {
        path.relative_to(sdk_attestation.PACKAGE_ROOT).as_posix()
        for path in sdk_attestation.attested_files()
    }
    assert covered, "the attested file set is empty"
    assert "src/mcp_heartbeat/issuer.py" in covered
    assert "src/mcp_heartbeat_current/sdk.py" in covered
    assert "tests/current/test_sdk_conformance.py" in covered
    # Excluded on purpose: the SDK venv never imports the conformance
    # package, so the run proves nothing about it — and including it
    # would mean the module that reads the record could not be edited
    # without invalidating it.
    assert not any(part.startswith("src/mcp_heartbeat_conformance") for part in covered)


def test_the_core_overrun_is_signed_off_without_the_measurement_moving(matrices) -> None:
    """D1: 430 is *accepted*, not made to fit.

    Both halves are asserted, because only one of them is the decision.
    The case clears — and the measurement is still 430 against a budget
    still set at 400. A version of this that cleared because the core had
    shrunk, or because the budget had grown, would be the failure mode
    the PRD's first hard constraint exists to catch.
    """
    case = next(c for c in matrices["isolation"].cases if c.case_id == "core-size")
    assert case.verdict is Verdict.PASS, case.reason

    # The measurement, unchanged.
    assert case.observations["measured"] == 430
    assert case.observations["prd_budget"] == 400
    assert case.observations["measured"] > case.observations["prd_budget"]
    assert case.observations["measured"] <= case.observations["recorded_ceiling"]

    # The decision, recorded against that exact number.
    signoff = case.observations["signoff"]
    assert signoff["decision"] == "accepted"
    assert signoff["measured_loc"] == 430
    assert signoff["prd_budget_loc"] == 400
    assert signoff["approver"] and signoff["approved_on"] and signoff["rationale"]
    assert case.observations["signoff_covers_this_measurement"] is True


def test_a_signoff_does_not_transfer_to_a_different_measurement() -> None:
    """The anti-gaming property, stated directly.

    A signoff for 430 must say nothing about 431. Otherwise "accept the
    overrun" becomes a standing waiver and the budget stops being a
    budget — which is the exact trade the PRD forbids.
    """
    from mcp_heartbeat_conformance.isolation import (
        PRD_CORE_BUDGET_LOC,
        load_core_size_signoff,
        signoff_covers,
    )

    signoff = load_core_size_signoff()
    assert signoff is not None, "the operator signoff record is missing"
    assert signoff_covers(signoff, 430) is True
    assert signoff_covers(signoff, 431) is False
    assert signoff_covers(signoff, 429) is False
    assert signoff_covers(None, 430) is False
    assert signoff_covers({**signoff, "decision": "deferred"}, 430) is False
    assert signoff_covers({**signoff, "prd_budget_loc": 430}, 430) is False
    assert signoff_covers({**signoff, "approver": ""}, 430) is False
    # And the budget the gate compares against is still the PRD's.
    assert PRD_CORE_BUDGET_LOC == 400


def test_the_two_eras_agree_on_a_policy_gap(matrices) -> None:
    """D2: one observable answer, fail-closed, on both eras."""
    case = next(
        c for c in matrices["identity-binding"].cases if c.case_id == "policy-gap-divergence"
    )
    assert case.verdict is Verdict.PASS, case.reason
    assert all(check["passed"] for check in case.checks)

    # Asserted on the *serialised* observations, because that is the form
    # that reaches the evidence pack — an operator reads the JSON, not
    # the live objects.
    observed = case.to_dict()["observations"]
    assert observed["current"]["identity_binding"] == "unbound"
    assert observed["legacy"]["identity_binding"] == "unbound"
    assert observed["current"]["identity_binding"] == observed["legacy"]["identity_binding"]
    # Same answer, and neither is authoritative.
    assert observed["current"]["authoritative"] is False
    # The diagnostic survived the normalization instead of being flattened with it.
    assert observed["current"]["reason"] == "no_policy_for_principal"
    assert observed["legacy"]["reason"] == "no_policy_for_principal"
    assert "security event, not missing evidence" in observed["decision"]


def test_the_diagnostic_reason_did_not_widen_the_wire_object(matrices) -> None:
    """D3: six fields, and the reason is not the seventh."""
    case = next(
        c for c in matrices["identity-binding"].cases if c.case_id == "wire-object-frozen"
    )
    assert case.verdict is Verdict.PASS, case.reason
    observed = case.to_dict()["observations"]
    assert observed["wire_field_count"] == 6
    assert observed["wire_fields"] == [
        "boot_id",
        "expires_at",
        "extension_version",
        "issued_at",
        "node_id",
        "sequence",
    ]
    # Every refusal still carries its reason — just not in there.
    assert all(observed["reasons_by_era"].values())


def test_nothing_is_open(matrices) -> None:
    """Nothing FAILs, nothing is on HOLD, nothing went unexercised.

    Stated over every case in every matrix rather than as three totals,
    so the failure message names the case that reopened.
    """
    open_cases = sorted(
        (mid, case.case_id, case.verdict.value)
        for mid, report in matrices.items()
        for case in report.cases
        if case.verdict in (Verdict.FAIL, Verdict.HOLD, Verdict.UNSUPPORTED)
    )
    assert open_cases == [], open_cases

    # And the run is not green by being empty.
    assert sum(len(report.cases) for report in matrices.values()) == 47


# ── residual risks and the gate ───────────────────────────────────────


def test_every_residual_risk_traces_to_an_open_case(report, matrices) -> None:
    listed = {(risk["matrix"], risk["case"]) for risk in report.residual_risks}
    open_cases = {
        (mid, case.case_id)
        for mid, matrix in matrices.items()
        for case in matrix.cases
        if case.verdict in (Verdict.FAIL, Verdict.HOLD, Verdict.UNSUPPORTED)
    }
    assert listed == open_cases


def test_risks_carry_a_severity_and_sort_worst_first(report) -> None:
    severities = [risk["severity"] for risk in report.residual_risks]
    assert set(severities) <= {"blocking", "decision-required", "unproven"}
    assert "blocking" not in severities, "a blocking risk means something FAILed"
    order = {"blocking": 0, "decision-required": 1, "unproven": 2}
    assert severities == sorted(severities, key=lambda s: order[s])


def test_the_run_takes_no_external_action(report) -> None:
    gate = report.to_dict()["gate"]
    assert gate["actions_taken"] == []
    assert gate["stopped_for_operator_review"] is True
    assert set(gate["gates"]) == set(RELEASE_GATES)
    assert set(gate["gates"].values()) == {"operator_approval_required"}


def test_the_gate_matrix_itself_passes() -> None:
    """The adjudicator is adjudicated."""
    gate = release.run()
    assert gate.count(Verdict.FAIL) == 0
    assert gate.ok is True
    assert {case.case_id for case in gate.cases} == {
        "levels-independent",
        "levels-can-differ",
        "risks-derived",
        "operator-gate",
    }


def test_a_synthetic_all_green_run_would_be_publish_ready(matrices) -> None:
    """The publish gate must be reachable, not merely strict.

    A gate nothing can ever pass is indistinguishable from a gate that
    is broken, so this proves the criteria are satisfiable by promoting
    every open case and re-adjudicating.
    """
    import copy

    green = copy.deepcopy(dict(matrices))
    for matrix in green.values():
        for case in matrix.cases:
            if case.verdict in (Verdict.HOLD, Verdict.UNSUPPORTED):
                case.verdict = Verdict.PASS
                case.reason = None

    publish = next(
        level for level in adjudicate(green).levels if level["key"] == "ready_to_publish"
    )
    assert publish["ready"] is True, publish["unmet"]
