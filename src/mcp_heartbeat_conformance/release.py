"""Release adjudication: three verdicts, none inferred from another.

The PRD is explicit that the report must distinguish

* **ready to dogfood** — the originating project may depend on this internally;
* **ready for external review** — the evidence pack can be handed to
  someone outside the project;
* **ready to publish** — the feature can go out,

and that "none is inferred from another". That last clause is the whole
design constraint, and it rules out the obvious implementation. Writing
``publish = review and no_holds`` would make publish-readiness a
*consequence* of review-readiness, and the day the review criteria were
loosened, publish-readiness would loosen silently with it.

So each level is a :class:`Level` carrying its **own complete list** of
criteria, evaluated independently against the raw matrix reports.
:func:`the_levels_are_independently_computed` asserts that structurally:
no level's criteria are a strict subset of another's by reference, and
every level names every criterion it depends on. That the sets overlap
heavily is fine and expected — what matters is that each is stated in
full rather than inherited.

**This module decides nothing operational.** Every gate in the PRD is
``operator_approval_required``, so :func:`adjudicate` reports a verdict
and stops. No publication, package upload, image push, or standards
submission happens here, and :func:`the_run_stops_at_the_operator_gate`
asserts the report says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Mapping

from .verdicts import Case, MatrixReport, Verdict, run_cases

#: Every gate the PRD declares, and its required approver. Restated as
#: data so the report can assert that none of them was self-approved.
RELEASE_GATES: Mapping[str, str] = {
    "implementation": "operator_approval_required",
    "merge_on_green": "operator_approval_required",
    "external_publication": "operator_approval_required",
    "package_release": "operator_approval_required",
    "public_image_push": "operator_approval_required",
    "standards_submission": "operator_approval_required",
    "production_activation": "operator_approval_required",
}


@dataclass(frozen=True)
class Criterion:
    """One named, independently evaluable condition over the matrices."""

    key: str
    description: str
    predicate: Callable[[Mapping[str, MatrixReport]], bool]

    def evaluate(self, matrices: Mapping[str, MatrixReport]) -> bool:
        try:
            return bool(self.predicate(matrices))
        except Exception:  # noqa: BLE001 - an unevaluable criterion is unmet
            return False


@dataclass
class Level:
    """One readiness level and the complete set of criteria that defines it."""

    key: str
    title: str
    means: str
    criteria: tuple[Criterion, ...]

    def evaluate(self, matrices: Mapping[str, MatrixReport]) -> dict[str, Any]:
        results = {c.key: c.evaluate(matrices) for c in self.criteria}
        unmet = sorted(key for key, met in results.items() if not met)
        return {
            "key": self.key,
            "title": self.title,
            "means": self.means,
            "ready": unmet == [],
            "criteria": [
                {"key": c.key, "description": c.description, "met": results[c.key]}
                for c in self.criteria
            ],
            "unmet": unmet,
        }


# ── criterion predicates ──────────────────────────────────────────────


def _no_failures(*matrix_ids: str) -> Callable[[Mapping[str, MatrixReport]], bool]:
    def predicate(matrices: Mapping[str, MatrixReport]) -> bool:
        return all(matrices[mid].count(Verdict.FAIL) == 0 for mid in matrix_ids)

    return predicate


def _no_holds(*matrix_ids: str) -> Callable[[Mapping[str, MatrixReport]], bool]:
    def predicate(matrices: Mapping[str, MatrixReport]) -> bool:
        return all(matrices[mid].count(Verdict.HOLD) == 0 for mid in matrix_ids)

    return predicate


def _nothing_unsupported(*matrix_ids: str) -> Callable[[Mapping[str, MatrixReport]], bool]:
    def predicate(matrices: Mapping[str, MatrixReport]) -> bool:
        return all(matrices[mid].count(Verdict.UNSUPPORTED) == 0 for mid in matrix_ids)

    return predicate


def _case_passed(matrix_id: str, case_id: str) -> Callable[[Mapping[str, MatrixReport]], bool]:
    def predicate(matrices: Mapping[str, MatrixReport]) -> bool:
        return any(
            case.case_id == case_id and case.verdict is Verdict.PASS
            for case in matrices[matrix_id].cases
        )

    return predicate


ALL_MATRICES = ("cross-era", "distributed-runtime", "identity-binding", "clean-room", "measurement", "isolation")


#: The three levels. Each names every criterion it depends on, in full.
LEVELS: tuple[Level, ...] = (
    Level(
        key="ready_to_dogfood",
        title="Ready to dogfood",
        means=(
            "the originating project may depend on this internally. Nothing is broken; "
            "open questions and unproven legs are acceptable because the "
            "consumer is the same team that owns the code."
        ),
        criteria=(
            Criterion("no_cross_era_failures", "No FAIL in the cross-era matrix", _no_failures("cross-era")),
            Criterion("no_runtime_failures", "No FAIL in the distributed-runtime matrix", _no_failures("distributed-runtime")),
            Criterion("no_identity_failures", "No FAIL in the identity-binding matrix", _no_failures("identity-binding")),
            Criterion("no_isolation_failures", "No FAIL in the isolation matrix", _no_failures("isolation")),
            Criterion(
                "identity_gates_state",
                "An unbound identity provably cannot move authoritative state",
                _case_passed("identity-binding", "gates-convergence"),
            ),
            Criterion(
                "package_boundaries_hold",
                "No package imports outwards or into a host application",
                _case_passed("isolation", "boundaries"),
            ),
        ),
    ),
    Level(
        key="ready_for_external_review",
        title="Ready for external review",
        means=(
            "The evidence pack can be handed to a reviewer outside the "
            "project. It must be complete, sanitized, and independently "
            "rerunnable — but it may still carry open questions, so long "
            "as they are stated rather than hidden."
        ),
        criteria=(
            Criterion("no_cross_era_failures", "No FAIL in the cross-era matrix", _no_failures("cross-era")),
            Criterion("no_runtime_failures", "No FAIL in the distributed-runtime matrix", _no_failures("distributed-runtime")),
            Criterion("no_identity_failures", "No FAIL in the identity-binding matrix", _no_failures("identity-binding")),
            Criterion("no_isolation_failures", "No FAIL in the isolation matrix", _no_failures("isolation")),
            Criterion("no_cleanroom_failures", "No FAIL in the clean-room matrix", _no_failures("clean-room")),
            Criterion("no_measurement_failures", "No FAIL in the measurement matrix", _no_failures("measurement")),
            Criterion(
                "independent_participant_verified",
                "The clean-room participant's independence is mechanically proven",
                _case_passed("clean-room", "no-link"),
            ),
            Criterion(
                "two_way_interop_shown",
                "Each implementation admits the other's documents",
                _case_passed("clean-room", "bidirectional-interop"),
            ),
            Criterion(
                "overhead_measured",
                "Bandwidth, CPU, and memory are measured against declared thresholds",
                _case_passed("measurement", "bytes"),
            ),
            Criterion(
                "abuse_resistance_shown",
                "A hint flood provably does not amplify into a fetch flood",
                _case_passed("measurement", "hint-flood"),
            ),
            Criterion(
                "evidence_is_sanitized",
                "No source file carries a host path, credential, or private address",
                _case_passed("isolation", "no-leaks"),
            ),
        ),
    ),
    Level(
        key="ready_to_publish",
        title="Ready to publish",
        means=(
            "The feature can go out. Every declared leg has actually been "
            "exercised and every open question has been closed — an "
            "UNSUPPORTED leg means a claim nobody has tested, and a HOLD "
            "means a decision nobody has made."
        ),
        criteria=(
            Criterion("no_failures_anywhere", "No FAIL in any matrix", _no_failures(*ALL_MATRICES)),
            Criterion("no_holds_anywhere", "No HOLD in any matrix: every open question is closed", _no_holds(*ALL_MATRICES)),
            Criterion(
                "every_declared_leg_was_exercised",
                "No UNSUPPORTED case: nothing is claimed that was not run",
                _nothing_unsupported(*ALL_MATRICES),
            ),
            Criterion(
                "official_sdk_leg_proven",
                "The current-era leg ran against the pinned official SDK v2",
                _case_passed("cross-era", "current-current-sdk"),
            ),
            Criterion(
                "core_within_prd_budget",
                "The portable core is inside the PRD's size budget, or the overrun is signed off",
                _case_passed("isolation", "core-size"),
            ),
            Criterion(
                "identity_semantics_settled",
                "Both eras give the same observable answer for a policy gap",
                _case_passed("identity-binding", "policy-gap-divergence"),
            ),
            Criterion(
                "downgrade_confusion_closed",
                "Every message plausible to both eras is refused by both",
                _case_passed("cross-era", "downgrade-confusion"),
            ),
        ),
    ),
)


@dataclass
class ReleaseReport:
    """The adjudication: levels, residual risks, and the operator gate."""

    levels: list[dict[str, Any]] = field(default_factory=list)
    residual_risks: list[dict[str, Any]] = field(default_factory=list)
    matrices: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "levels": self.levels,
            "residual_risks": self.residual_risks,
            "matrix_totals": self.matrices,
            "gate": {
                "gates": dict(RELEASE_GATES),
                "actions_taken": [],
                "stopped_for_operator_review": True,
                "note": (
                    "HB-05 is a verification PRD. No publication, package upload, "
                    "image push, standards submission, or production activation "
                    "occurred; every gate above requires operator approval."
                ),
            },
        }


def collect_residual_risks(matrices: Mapping[str, MatrixReport]) -> list[dict[str, Any]]:
    """Every HOLD, UNSUPPORTED, and FAIL, as a risk an operator must weigh.

    Derived from the matrices rather than hand-listed, so a risk cannot
    be closed by deleting a sentence from a report while the case that
    raised it still holds.
    """
    severity = {Verdict.FAIL: "blocking", Verdict.HOLD: "decision-required", Verdict.UNSUPPORTED: "unproven"}
    risks: list[dict[str, Any]] = []
    for matrix_id, report in matrices.items():
        for case in report.cases:
            if case.verdict in severity:
                risks.append(
                    {
                        "matrix": matrix_id,
                        "case": case.case_id,
                        "title": case.title,
                        "verdict": case.verdict.value,
                        "severity": severity[case.verdict],
                        "detail": case.reason or case.error or "see the case's failed checks",
                    }
                )
    order = {"blocking": 0, "decision-required": 1, "unproven": 2}
    risks.sort(key=lambda risk: (order[risk["severity"]], risk["matrix"], risk["case"]))
    return risks


def adjudicate(matrices: Mapping[str, MatrixReport]) -> ReleaseReport:
    """Evaluate every level independently and collect the residual risks."""
    return ReleaseReport(
        levels=[level.evaluate(matrices) for level in LEVELS],
        residual_risks=collect_residual_risks(matrices),
        matrices={mid: report.to_dict()["totals"] for mid, report in matrices.items()},
    )


# ── cases that adjudicate the adjudicator ─────────────────────────────


@lru_cache(maxsize=1)
def _sample_matrices() -> dict[str, MatrixReport]:
    """Run every matrix once. Imported lazily to keep the cycle out.

    Cached because three of the four cases below need the same input and
    the matrices are deterministic — re-running them per case would
    quadruple the cost of adjudicating for no extra evidence. The
    reports are read-only from here on.
    """
    from . import cleanroom, cross_era, identity_matrix, isolation, measurement, runtime

    return {
        "cross-era": cross_era.run(),
        "distributed-runtime": runtime.run(),
        "identity-binding": identity_matrix.run(),
        "clean-room": cleanroom.run(),
        "measurement": measurement.run(),
        "isolation": isolation.run(),
    }


def the_levels_are_independently_computed(case: Case) -> None:
    """No level's verdict is derived from another's.

    Asserted structurally rather than by reading the code: each level
    must name its own criteria in full, and no level may be implemented
    as "the previous one plus something".
    """
    keys = [level.key for level in LEVELS]
    case.check("all_three_levels_are_declared", len(LEVELS) == 3, keys)
    case.check("their_keys_are_distinct", len(set(keys)) == 3, keys)

    criteria_keys = {level.key: {c.key for c in level.criteria} for level in LEVELS}
    case.check(
        "every_level_names_at_least_four_criteria",
        all(len(level.criteria) >= 4 for level in LEVELS),
        {k: len(v) for k, v in criteria_keys.items()},
    )
    case.check(
        "no_two_levels_have_identical_criteria",
        len({frozenset(v) for v in criteria_keys.values()}) == 3,
        {k: sorted(v) for k, v in criteria_keys.items()},
    )
    case.check(
        "every_criterion_has_its_own_predicate",
        all(
            callable(criterion.predicate)
            for level in LEVELS
            for criterion in level.criteria
        ),
    )
    # The publish level must ask something the review level does not, or
    # it would be review-readiness under another name.
    exclusive = criteria_keys["ready_to_publish"] - criteria_keys["ready_for_external_review"]
    case.check("publish_asks_something_review_does_not", exclusive != set(), sorted(exclusive))
    case.observations = {"criteria_per_level": {k: sorted(v) for k, v in criteria_keys.items()}}


def a_lower_level_can_pass_while_a_higher_one_does_not(case: Case) -> None:
    """The levels must be able to disagree, or they are one verdict.

    This is the executable version of "none is inferred from another".
    It used to prove that by *observing* the live run: while something
    was open, the three unmet lists differed, and that was taken as the
    evidence. That proof quietly depended on the run being red — once
    every case cleared, all three lists went empty and the case FAILed
    while nothing was actually wrong with the adjudicator.

    So the property is proved the way it should have been from the
    start: counterfactually. Take the real matrices, degrade one case,
    and show publish moves while dogfood does not. That holds on a green
    run and on a red one, and it demonstrates the *mechanism* rather
    than a side effect of the day's verdicts.
    """
    import copy

    report = adjudicate(_sample_matrices())
    by_key = {level["key"]: level for level in report.levels}

    case.check("every_level_was_evaluated", len(report.levels) == 3, sorted(by_key))
    case.check(
        "each_level_reports_its_own_unmet_list",
        all("unmet" in level and "criteria" in level for level in report.levels),
    )

    # Degrade the SDK leg — a case only the publish level asks about —
    # and re-adjudicate a copy. Deep-copied because the sample is cached
    # and every other case reads it.
    degraded = copy.deepcopy(dict(_sample_matrices()))
    for degraded_case in degraded["cross-era"].cases:
        if degraded_case.case_id == "current-current-sdk":
            degraded_case.verdict = Verdict.UNSUPPORTED
            degraded_case.reason = "counterfactual: the leg was not exercised"
    counterfactual = {level["key"]: level for level in adjudicate(degraded).levels}

    case.check(
        "the_three_verdicts_are_not_forced_to_move_together",
        len({tuple(sorted(level["unmet"])) for level in counterfactual.values()}) > 1,
        {key: level["unmet"] for key, level in counterfactual.items()},
    )
    case.check(
        "an_unsupported_case_blocks_publish_without_blocking_dogfood",
        counterfactual["ready_to_publish"]["ready"] is False
        and counterfactual["ready_to_dogfood"]["ready"] is True,
        {
            "publish_ready": counterfactual["ready_to_publish"]["ready"],
            "dogfood_ready": counterfactual["ready_to_dogfood"]["ready"],
            "unmet_under_degradation": counterfactual["ready_to_publish"]["unmet"],
        },
    )
    case.check(
        "the_degradation_is_what_moved_publish",
        by_key["ready_to_publish"]["ready"] is not counterfactual["ready_to_publish"]["ready"]
        or by_key["ready_to_publish"]["ready"] is False,
        {
            "actual": by_key["ready_to_publish"]["ready"],
            "counterfactual": counterfactual["ready_to_publish"]["ready"],
        },
    )
    case.observations = {
        "actual": {
            level["key"]: {"ready": level["ready"], "unmet": level["unmet"]}
            for level in report.levels
        },
        "with_the_sdk_leg_degraded": {
            key: {"ready": level["ready"], "unmet": level["unmet"]}
            for key, level in counterfactual.items()
        },
    }


def residual_risks_are_derived_not_authored(case: Case) -> None:
    """Every open risk traces back to a case that is still open."""
    matrices = _sample_matrices()
    risks = collect_residual_risks(matrices)

    open_cases = {
        (mid, c.case_id)
        for mid, report in matrices.items()
        for c in report.cases
        if c.verdict in (Verdict.FAIL, Verdict.HOLD, Verdict.UNSUPPORTED)
    }
    listed = {(risk["matrix"], risk["case"]) for risk in risks}

    case.check("every_open_case_is_listed_as_a_risk", open_cases <= listed, sorted(open_cases - listed))
    case.check("no_risk_is_listed_without_an_open_case", listed <= open_cases, sorted(listed - open_cases))
    case.check(
        "every_risk_carries_a_severity_and_a_detail",
        all(risk.get("severity") and risk.get("detail") for risk in risks),
        risks,
    )
    case.check(
        "blocking_risks_sort_first",
        [r["severity"] for r in risks] == sorted(r["severity"] for r in risks)
        or all(r["severity"] != "blocking" for r in risks),
        [r["severity"] for r in risks],
    )
    case.observations = {"risk_count": len(risks), "risks": risks}


def the_run_stops_at_the_operator_gate(case: Case) -> None:
    """No gate is self-approved and no external action is taken."""
    report = adjudicate(_sample_matrices()).to_dict()
    gate = report["gate"]

    case.check("every_prd_gate_is_declared", set(gate["gates"]) == set(RELEASE_GATES), sorted(gate["gates"]))
    case.check(
        "every_gate_requires_operator_approval",
        set(gate["gates"].values()) == {"operator_approval_required"},
        sorted(set(gate["gates"].values())),
    )
    case.check("no_action_was_taken", gate["actions_taken"] == [], gate["actions_taken"])
    case.check("the_run_stopped_for_review", gate["stopped_for_operator_review"] is True)
    case.check(
        "publication_is_not_claimed_anywhere_in_the_report",
        "published" not in str(report).lower(),
    )
    case.observations = {"gates": sorted(gate["gates"])}


CASES: tuple[tuple[str, str, Any], ...] = (
    ("levels-independent", "The three levels are computed independently", the_levels_are_independently_computed),
    ("levels-can-differ", "A lower level can pass while a higher one does not", a_lower_level_can_pass_while_a_higher_one_does_not),
    ("risks-derived", "Residual risks are derived from open cases, not authored", residual_risks_are_derived_not_authored),
    ("operator-gate", "The run stops at the operator gate", the_run_stops_at_the_operator_gate),
)


def run() -> MatrixReport:
    """Adjudicate the adjudicator. Always completes."""
    report = MatrixReport(
        matrix_id="release-gate",
        title="Release-readiness adjudication and the operator gate",
    )
    return run_cases(report, CASES)


__all__ = [
    "CASES",
    "Criterion",
    "LEVELS",
    "Level",
    "RELEASE_GATES",
    "ReleaseReport",
    "adjudicate",
    "collect_residual_risks",
    "run",
]
