"""Generate the HB-05 evidence pack.

Every artifact is produced from the *running* code rather than
transcribed, so an evidence file cannot describe a version of the
adapter that no longer exists. Run it and the pack regenerates; that is
what "independently rerunnable" means in the acceptance criteria.

    python tools/emit_hb05_evidence.py --output docs/evidence/mcp-heartbeat-hb05

The pack is sanitized by construction rather than by a redaction pass:
nothing here reads an environment variable, a home directory, a git
remote, or a hostname, and :mod:`mcp_heartbeat_conformance.isolation`
scans the whole tree for leaks as one of the matrices. ``--check`` runs
the scan over the *generated pack* as well, so a leak introduced by a
future case is caught before the file lands.

Exit codes: ``0`` when every matrix cleared, ``1`` when something FAILed
or is on HOLD, ``2`` on a usage error. A non-zero exit is not a crash —
it is the gate saying an operator has to look.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

import mcp_heartbeat_conformance as hb5  # noqa: E402
from mcp_heartbeat_conformance import isolation, measurement, release  # noqa: E402
from mcp_heartbeat_current import contract, sdk  # noqa: E402

EVIDENCE_SCHEMA_VERSION = "0.1"


def environment() -> dict[str, Any]:
    """What is needed to rerun this, and nothing that identifies a host."""
    return {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "conformance_version": hb5.__version__,
        "protocol_revision": contract.PROTOCOL_REVISION,
        "extension_id": contract.HEARTBEAT_EXTENSION_ID,
        "extension_version": contract.HEARTBEAT_EXTENSION_VERSION,
        "official_sdk": {
            "available": sdk.SDK_AVAILABLE,
            "pin": contract.SDK_PIN.to_dict(),
        },
    }


def build_pack() -> dict[str, dict[str, Any]]:
    """Every artifact in the pack, keyed by filename."""
    matrices = hb5.run_matrices()
    gate = release.run()
    adjudication = release.adjudicate(matrices)

    artifacts: dict[str, dict[str, Any]] = {}
    for matrix_id, report in matrices.items():
        artifacts[f"matrix-{matrix_id}.json"] = report.to_dict()
    artifacts["matrix-release-gate.json"] = gate.to_dict()

    artifacts["measurements.json"] = {
        "artifact": "measured-overhead",
        "note": (
            "Bytes are exact and machine-independent. CPU and memory figures "
            "come from the run that produced this file and are compared "
            "against the thresholds in mcp_heartbeat_conformance.measurement."
        ),
        **measurement.run_measurements_only(),
    }

    artifacts["release-report.json"] = {
        "artifact": "operator-release-gate",
        **adjudication.to_dict(),
    }

    artifacts["summary.json"] = {
        "artifact": "hb05-summary",
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "environment": environment(),
        "totals": {
            matrix_id: report.to_dict()["totals"] for matrix_id, report in matrices.items()
        },
        "release_gate_totals": gate.to_dict()["totals"],
        "readiness": {
            level["key"]: {"ready": level["ready"], "unmet": level["unmet"]}
            for level in adjudication.levels
        },
        "residual_risks": adjudication.residual_risks,
        "ok": all(report.ok for report in matrices.values()) and gate.ok,
    }
    return artifacts


def scan_pack(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Run the structural leak scan over the generated pack itself."""
    findings: dict[str, Any] = {}
    for name, payload in artifacts.items():
        leaks = isolation.scan_for_leaks(json.dumps(payload, sort_keys=True))
        if leaks:
            findings[name] = leaks
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="directory to write the pack into")
    parser.add_argument(
        "--check",
        action="store_true",
        help="scan the generated pack for leaks and refuse to write if any are found",
    )
    args = parser.parse_args(argv)

    artifacts = build_pack()
    summary = artifacts["summary.json"]

    if args.check or args.output:
        leaks = scan_pack(artifacts)
        if leaks:
            print("refusing to write: the pack contains structural leaks", file=sys.stderr)
            print(json.dumps(leaks, indent=2), file=sys.stderr)
            return 2

    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        for name, payload in artifacts.items():
            path = args.output / name
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"wrote {path}")
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))

    for matrix_id, totals in summary["totals"].items():
        print(
            f"[{matrix_id}] {totals['passed']}/{totals['total']} passed, "
            f"{totals['failed']} failed, {totals['hold']} hold, "
            f"{totals['unsupported']} unsupported",
            file=sys.stderr,
        )
    for key, level in summary["readiness"].items():
        state = "READY" if level["ready"] else "NOT READY"
        print(f"[{key}] {state}" + (f" — unmet: {', '.join(level['unmet'])}" if level["unmet"] else ""), file=sys.stderr)

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
