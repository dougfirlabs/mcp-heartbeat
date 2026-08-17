"""Write the attestation for an official-SDK conformance run.

Runs *inside* the throwaway venv that ``tools/verify_sdk.sh`` builds, at
the end of the run it is recording, and only there: it re-derives the
contract from the SDK that is actually importable in this interpreter,
so a record can never describe an SDK the run did not use.

    tools/record_sdk_verification.py --junit-xml /tmp/x.xml --recorded-by mcp-hb-x3

The counts come from pytest's own JUnit XML rather than from a scraped
summary line, because the summary line is prose and the XML is the
report. Refuses to write anything if the SDK is absent, if the installed
distributions differ from the pin, or if the suite recorded a failure or
an error — the point is a transcript of a passing run, and there is no
such thing as a partial one.

Exit codes: ``0`` on a written record, ``1`` when the run does not
warrant one, ``2`` on a usage error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from xml.etree import ElementTree

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from mcp_heartbeat_conformance import sdk_attestation  # noqa: E402
from mcp_heartbeat_current import contract, sdk  # noqa: E402


def suite_counts(junit_xml: Path) -> dict[str, int]:
    """Totals from pytest's JUnit report.

    ``tests`` counts every case the suite collected; ``passed`` is what
    is left after the ones that did not. Skips are reported rather than
    folded into either, so a suite that skipped the SDK cases cannot
    read as one that ran them.
    """
    root = ElementTree.parse(junit_xml).getroot()
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))

    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return totals


def build_record(counts: dict[str, int], recorded_by: str, recorded_on: str) -> dict[str, object]:
    """The record itself, derived from this interpreter and this run.

    :func:`~mcp_heartbeat_current.sdk.assert_contract_matches_sdk` is
    called rather than trusted: it raises on the first constant that has
    drifted, so a record can only be built in an environment where the
    contract and the installed SDK still agree.
    """
    provenance = sdk.assert_contract_matches_sdk()
    installed = provenance["sdk"]["installed"]

    return {
        "record": sdk_attestation.ATTESTATION_RECORD,
        "sdk_distribution": contract.SDK_PIN.distribution,
        "sdk_version": installed[contract.SDK_PIN.distribution],
        "sdk_types_distribution": contract.SDK_PIN.types_distribution,
        "sdk_types_version": installed[contract.SDK_PIN.types_distribution],
        "implements_revision": provenance["sdk"]["latest_modern_version"],
        "matches_pin": provenance["sdk"]["matches_pin"],
        "suite": "tests/current",
        "tests_collected": counts["tests"],
        "tests_passed": counts["passed"],
        "tests_failed": counts["failures"] + counts["errors"],
        "tests_skipped": counts["skipped"],
        "constants_rederived": len(provenance["constants"]),
        "identifier_grammar_cases": len(provenance["extension_identifier_grammar"]),
        "adapter_digest": sdk_attestation.adapter_digest(),
        "attested_trees": list(sdk_attestation.ATTESTED_TREES),
        "recorded_by": recorded_by,
        "recorded_on": recorded_on,
        "scope": (
            "This record attests one run of tests/current against the pinned "
            "official SDK, in an isolated venv, over the trees named in "
            "attested_trees. It is keyed to adapter_digest, so it lapses the "
            "moment any of those sources changes -- it cannot be inherited by "
            "a different tree, a different SDK, or a later edit."
        ),
        "invalidated_if": [
            "any file under the attested trees is added, removed, moved, or edited",
            f"the installed SDK is anything other than {contract.SDK_PIN.distribution}"
            f"=={contract.SDK_PIN.version}",
            "the recorded suite reports any failure or error",
        ],
        "does_not_authorize": [
            "treating any other matrix case as exercised",
            "external publication, package upload, image push, or standards submission",
        ],
        "note": (
            "Written by the run it describes. No path, host, or interpreter "
            "location is recorded: the versions, the counts, and the digest "
            "are what make the leg reproducible, and nothing else here would."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit-xml", type=Path, required=True, help="pytest JUnit report for the run")
    parser.add_argument("--recorded-by", required=True, help="what produced this run")
    parser.add_argument("--recorded-on", required=True, help="UTC date of the run, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=sdk_attestation.SDK_ATTESTATION_PATH)
    args = parser.parse_args(argv)

    if not sdk.SDK_AVAILABLE:
        print(
            "refusing to record: the official SDK is not importable in this "
            "interpreter, so there is no run to attest",
            file=sys.stderr,
        )
        return 1
    if not args.junit_xml.is_file():
        print(f"refusing to record: no JUnit report at {args.junit_xml.name}", file=sys.stderr)
        return 2

    counts = suite_counts(args.junit_xml)
    if counts["failures"] or counts["errors"]:
        print(
            f"refusing to record: the run had {counts['failures']} failures and "
            f"{counts['errors']} errors; an attestation is a transcript of a passing run",
            file=sys.stderr,
        )
        return 1
    if counts["passed"] <= 0:
        print("refusing to record: the run passed nothing", file=sys.stderr)
        return 1

    record = build_record(counts, args.recorded_by, args.recorded_on)
    if not sdk_attestation.attestation_covers(record, sdk_attestation.adapter_digest()):
        print(
            "refusing to record: the record it produced does not cover this "
            "tree, which means it would not clear the case it exists for",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output.name}: {record['tests_passed']} passed against "
          f"{record['sdk_distribution']}=={record['sdk_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
