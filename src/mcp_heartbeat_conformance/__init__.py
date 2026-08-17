"""HB-05 cross-era conformance and release adjudication.

Six matrices, one release report, and nothing operational:

``cross-era``
    The declared client/server era pairs and their confusable
    neighbours — :mod:`.cross_era`.
``distributed-runtime``
    Replicas, gateways, cold starts, rolling deploys, partitions, and
    suspension — :mod:`.runtime`.
``identity-binding``
    ``bound`` / ``unbound`` / ``unverified`` on both eras —
    :mod:`.identity_matrix`.
``clean-room``
    Provenance and two-way interoperability of the independent
    participant — :mod:`.cleanroom`.
``measurement``
    Bandwidth, CPU, memory, emission rate, and abuse resistance —
    :mod:`.measurement`.
``isolation``
    Package boundaries, core size, and the security/privacy/IP scans —
    :mod:`.isolation`.

:func:`run_all` runs every matrix and adjudicates. It takes no action:
every release gate is ``operator_approval_required``, so the run stops
at a verdict. See :mod:`.release`.

This package depends on the core and both era adapters and is depended
on by neither, so it can be removed without touching what it verifies.
"""
from __future__ import annotations

from typing import Any

from . import cleanroom, cross_era, identity_matrix, isolation, measurement, release, runtime
from .release import ReleaseReport, adjudicate
from .verdicts import CLEARING_VERDICTS, Case, MatrixReport, Verdict, run_cases

__version__ = "0.1.0"

#: Matrix id → the module that produces it, in reporting order.
MATRICES = {
    "cross-era": cross_era,
    "distributed-runtime": runtime,
    "identity-binding": identity_matrix,
    "clean-room": cleanroom,
    "measurement": measurement,
    "isolation": isolation,
}


def run_matrices() -> dict[str, MatrixReport]:
    """Run every conformance matrix. Always completes; failures are data."""
    return {matrix_id: module.run() for matrix_id, module in MATRICES.items()}


def run_all() -> dict[str, Any]:
    """Every matrix plus the release adjudication, as one JSON-able record."""
    matrices = run_matrices()
    adjudication = adjudicate(matrices)
    return {
        "schema_version": "0.1",
        "matrices": {mid: report.to_dict() for mid, report in matrices.items()},
        "release": adjudication.to_dict(),
    }


__all__ = [
    "CLEARING_VERDICTS",
    "Case",
    "MATRICES",
    "MatrixReport",
    "ReleaseReport",
    "Verdict",
    "__version__",
    "adjudicate",
    "cleanroom",
    "cross_era",
    "identity_matrix",
    "isolation",
    "measurement",
    "release",
    "run_all",
    "run_cases",
    "run_matrices",
    "runtime",
]
