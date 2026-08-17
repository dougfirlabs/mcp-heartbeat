"""Every HB-05 matrix runs, completes, and reports honestly.

These tests are deliberately *not* "assert everything passed". A
conformance harness whose test suite demanded green would be unable to
report a HOLD, and the HOLD is the most valuable thing it produces. So
the assertions here are about the properties that must hold whatever the
verdicts turn out to be:

* every declared case produces a verdict, and no case is silently
  skipped;
* nothing FAILs — a FAIL is a defect, unlike a HOLD or an UNSUPPORTED;
* the verdict vocabulary is used correctly (an UNSUPPORTED or HOLD case
  carries a reason; a PASS case does not);
* every report is JSON-serialisable, because an unserialisable
  measurement never reaches the evidence pack.

The specific open items of the day — the absent SDK, the LOC overrun,
the policy-gap divergence — are asserted by name in
:mod:`test_release_gate`, where changing one is supposed to be a
deliberate act.
"""
from __future__ import annotations

import json

import pytest

import mcp_heartbeat_conformance as hb5
from mcp_heartbeat_conformance import (
    cleanroom,
    cross_era,
    identity_matrix,
    isolation,
    measurement,
    release,
    runtime,
)
from mcp_heartbeat_conformance.verdicts import Case, MatrixReport, Verdict, run_cases

MODULES = (
    ("cross-era", cross_era),
    ("distributed-runtime", runtime),
    ("identity-binding", identity_matrix),
    ("clean-room", cleanroom),
    ("measurement", measurement),
    ("isolation", isolation),
    ("release-gate", release),
)


@pytest.fixture(scope="module")
def matrices() -> dict[str, MatrixReport]:
    """Every matrix, run once for the whole module."""
    return hb5.run_matrices()


@pytest.mark.parametrize("matrix_id,module", MODULES, ids=[m[0] for m in MODULES])
def test_every_matrix_runs_every_case_it_declares(matrix_id, module) -> None:
    report = module.run()
    assert report.matrix_id == matrix_id
    assert len(report.cases) == len(module.CASES), "a declared case did not run"
    assert [case.case_id for case in report.cases] == [c[0] for c in module.CASES]


@pytest.mark.parametrize("matrix_id,module", MODULES, ids=[m[0] for m in MODULES])
def test_no_matrix_contains_a_failure(matrix_id, module) -> None:
    """FAIL is a defect. HOLD and UNSUPPORTED are not, and are allowed."""
    report = module.run()
    failures = [
        {"case": case.case_id, "error": case.error, "failed_checks": [
            check["name"] for check in case.checks if not check["passed"]
        ]}
        for case in report.cases
        if case.verdict is Verdict.FAIL
    ]
    assert failures == [], json.dumps(failures, indent=2)


@pytest.mark.parametrize("matrix_id,module", MODULES, ids=[m[0] for m in MODULES])
def test_every_case_records_at_least_one_check_or_a_reason(matrix_id, module) -> None:
    """A case with no checks and no reason asserted nothing at all."""
    empty = [
        case.case_id
        for case in module.run().cases
        if not case.checks and case.reason is None and case.error is None
    ]
    assert empty == [], f"{matrix_id}: cases that asserted nothing: {empty}"


@pytest.mark.parametrize("matrix_id,module", MODULES, ids=[m[0] for m in MODULES])
def test_the_verdict_vocabulary_is_used_correctly(matrix_id, module) -> None:
    for case in module.run().cases:
        if case.verdict in (Verdict.UNSUPPORTED, Verdict.HOLD):
            assert case.reason, f"{case.case_id}: {case.verdict.value} without a reason"
        if case.verdict is Verdict.PASS:
            assert case.reason is None and case.error is None
            assert all(check["passed"] for check in case.checks)


@pytest.mark.parametrize("matrix_id,module", MODULES, ids=[m[0] for m in MODULES])
def test_every_report_is_json_serialisable(matrix_id, module) -> None:
    """An unserialisable observation never reaches the evidence pack."""
    payload = module.run().to_dict()
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload


def test_the_totals_add_up(matrices) -> None:
    for matrix_id, report in matrices.items():
        totals = report.to_dict()["totals"]
        assert (
            totals["passed"] + totals["failed"] + totals["unsupported"] + totals["hold"]
            == totals["total"]
        ), f"{matrix_id} totals do not sum"


def test_run_all_produces_a_complete_record(matrices) -> None:
    record = hb5.run_all()
    assert set(record["matrices"]) == set(hb5.MATRICES)
    assert record["schema_version"] == "0.1"
    assert {level["key"] for level in record["release"]["levels"]} == {
        "ready_to_dogfood",
        "ready_for_external_review",
        "ready_to_publish",
    }
    assert json.loads(json.dumps(record, sort_keys=True)) == record


# ── the verdict vocabulary itself ─────────────────────────────────────


def test_a_failed_check_turns_a_case_red() -> None:
    case = Case(case_id="x", title="x")
    assert case.verdict is Verdict.PASS
    case.check("ok", True)
    assert case.verdict is Verdict.PASS
    case.check("not_ok", False, "detail")
    assert case.verdict is Verdict.FAIL


def test_unsupported_is_not_a_pass_and_not_a_failure() -> None:
    case = Case(case_id="x", title="x")
    case.unsupported("no modern leg in this topology")
    assert case.verdict is Verdict.UNSUPPORTED
    assert not case.passed
    assert case.to_dict()["reason"] == "no modern leg in this topology"


def test_hold_blocks_a_matrix_while_unsupported_does_not() -> None:
    """The asymmetry the release report depends on."""
    held = MatrixReport(matrix_id="m", title="m")
    case = Case(case_id="x", title="x")
    case.hold("needs a decision")
    held.add(case)
    assert held.ok is False

    unsupported = MatrixReport(matrix_id="m", title="m")
    other = Case(case_id="x", title="x")
    other.unsupported("not expressible here")
    unsupported.add(other)
    assert unsupported.ok is True


def test_a_case_that_raises_becomes_a_failure_without_stopping_the_matrix() -> None:
    """One broken case must not truncate the report."""

    def explodes(case: Case) -> None:
        raise RuntimeError("boom")

    def fine(case: Case) -> None:
        case.check("ok", True)

    report = run_cases(
        MatrixReport(matrix_id="m", title="m"),
        (("bad", "explodes", explodes), ("good", "fine", fine)),
    )
    assert [case.verdict for case in report.cases] == [Verdict.FAIL, Verdict.PASS]
    assert "RuntimeError: boom" in (report.cases[0].error or "")


def test_observations_are_coerced_into_json() -> None:
    """Enums, dataclasses, and datetimes all survive the boundary."""
    case = Case(case_id="x", title="x")
    case.observations = {"verdict": Verdict.HOLD, "nested": [Verdict.PASS, {"k": Verdict.FAIL}]}
    payload = case.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["observations"]["verdict"] == "HOLD"
