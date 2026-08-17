"""The verdict vocabulary every HB-05 matrix reports in.

One shared vocabulary, because the release report adds these up across
five separate matrices and a per-matrix dialect would make the total
meaningless.

The vocabulary has four values rather than two, and each extra one
exists to stop a specific lie:

``UNSUPPORTED``
    The topology could not express the case. Not a pass — a run that
    declared everything unsupported must not read as green.
``HOLD``
    The case was expressible and was measured, but the measurement sits
    outside its declared threshold. Distinct from ``FAIL`` because a
    budget overrun is an operator decision, not a defect.

Nothing here raises. A conformance harness that stopped at the first
problem would report the first problem and hide the other eleven, so
every runner completes and failures are data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNSUPPORTED = "UNSUPPORTED"
    HOLD = "HOLD"


#: Verdicts that permit a release decision to proceed. ``HOLD`` is
#: deliberately absent: it is the value that stops one.
CLEARING_VERDICTS: frozenset[Verdict] = frozenset({Verdict.PASS, Verdict.UNSUPPORTED})


@dataclass
class Case:
    """One declared case, its verdict, and the checks behind it."""

    case_id: str
    title: str
    verdict: Verdict = Verdict.PASS
    checks: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    error: str | None = None

    def check(self, name: str, condition: bool, detail: Any = None) -> bool:
        """Record one named assertion. Returns the condition unchanged."""
        passed = bool(condition)
        self.checks.append({"name": name, "passed": passed, "detail": _plain(detail)})
        if not passed and self.verdict is Verdict.PASS:
            self.verdict = Verdict.FAIL
        return passed

    def unsupported(self, reason: str) -> None:
        """This topology cannot express the case, and here is why."""
        self.verdict = Verdict.UNSUPPORTED
        self.reason = reason

    def hold(self, reason: str) -> None:
        """Measured, outside threshold, and therefore an operator's call."""
        self.verdict = Verdict.HOLD
        self.reason = reason

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "error": self.error,
            "checks": self.checks,
            "observations": _plain(self.observations),
        }


@dataclass
class MatrixReport:
    """Every case in one matrix, plus the counts an operator reads first."""

    matrix_id: str
    title: str
    cases: list[Case] = field(default_factory=list)

    def add(self, case: Case) -> Case:
        self.cases.append(case)
        return case

    def count(self, verdict: Verdict) -> int:
        return sum(1 for case in self.cases if case.verdict is verdict)

    @property
    def ok(self) -> bool:
        """True when nothing failed and nothing is on hold.

        ``UNSUPPORTED`` does not block: an inexpressible case is reported,
        counted, and left for the release report to weigh. It is the
        release report — not this property — that decides whether the
        *number* of unsupported cases is acceptable.
        """
        return all(case.verdict in CLEARING_VERDICTS for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matrix_id": self.matrix_id,
            "title": self.title,
            "ok": self.ok,
            "totals": {
                "total": len(self.cases),
                "passed": self.count(Verdict.PASS),
                "failed": self.count(Verdict.FAIL),
                "unsupported": self.count(Verdict.UNSUPPORTED),
                "hold": self.count(Verdict.HOLD),
            },
            "cases": [case.to_dict() for case in self.cases],
        }


def run_cases(
    report: MatrixReport, cases: Iterable[tuple[str, str, Any]]
) -> MatrixReport:
    """Run ``(case_id, title, fn)`` triples, turning a raise into a FAIL.

    A case that explodes still returns a verdict, so one broken case
    cannot truncate the matrix and leave the remainder unreported.
    """
    for case_id, title, fn in cases:
        case = Case(case_id=case_id, title=title)
        try:
            fn(case)
        except Exception as exc:  # noqa: BLE001 - a crash is a FAIL, not a stop
            case.verdict = Verdict.FAIL
            case.error = f"{type(exc).__name__}: {exc}"
        report.add(case)
    return report


def _plain(value: Any) -> Any:
    """Coerce a value into something ``json.dumps`` will accept.

    Evidence has to be machine-readable, and the adapters hand back
    enums, dataclasses, and datetimes. Converting at the boundary keeps
    every call site from remembering to.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "CLEARING_VERDICTS",
    "Case",
    "MatrixReport",
    "Verdict",
    "run_cases",
]
