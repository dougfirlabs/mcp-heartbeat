"""The record of an SDK conformance run that happened somewhere else.

The current-era leg has to be proven against the pinned official SDK v2,
and that SDK cannot be installed where the evidence is generated:
``mcp==2.0.0`` resolves ``pydantic-core`` to a version other than the one
a host application's installed ``pydantic`` pins, so installing it would mutate an
environment the whole repository depends on. ``tools/verify_sdk.sh``
therefore runs the leg in a throwaway venv of its own — and this module
is how that run gets back to the matrix without anybody retyping it.

**Why this is not a reclassification.** An attestation is not a claim
that the leg passed; it is a *transcript* of a run that did, written by
the run itself, and it only clears the case while three things hold at
once: the recorded distributions equal the pin, the recorded suite had
failures zero and passed above zero, and :func:`adapter_digest` over the
current tree equals the digest recorded at the time. That last one is
the load-bearing clause. The digest covers exactly the sources the SDK
run exercised — the portable core, the current adapter, and
``tests/current/`` — so editing any line of them lapses the record and
:func:`~mcp_heartbeat_conformance.cross_era.current_to_current_over_official_sdk`
goes back to UNSUPPORTED. The attestation cannot outlive the code it
attests to, which is the same non-transferability property the core-size
signoff has against its ``(430, 400)`` pair.

Deliberately *not* recorded: the venv path, the interpreter path, the
hostname, or anything else that would make the pack describe a machine.
The versions, the counts and the digest are the whole of it, and
``emit_hb05_evidence.py --check`` scans the generated pack to enforce
that.

This module holds no SDK import. It runs in the environment without one,
which is the environment that needs to read the record.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from mcp_heartbeat_current import contract

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: Where ``tools/verify_sdk.sh`` deposits the transcript of its run.
SDK_ATTESTATION_PATH = PACKAGE_ROOT / "docs" / "sdk-verification.json"

#: The record kind, so a file that is something else cannot be read as one.
ATTESTATION_RECORD = "official-sdk-leg-verification"

#: The trees the isolated run actually exercises, relative to the package
#: root. ``tests/current/`` is in the set because the run's meaning is
#: "these assertions passed against that SDK" — a changed assertion makes
#: the recorded counts describe a suite that no longer exists.
#:
#: The conformance package is deliberately *out* of the set. The SDK venv
#: never imports it, so it proves nothing about it; including it would
#: also mean this very module could not be edited without invalidating
#: the record it reads.
ATTESTED_TREES: tuple[str, ...] = (
    "src/mcp_heartbeat",
    "src/mcp_heartbeat_current",
    "tests/current",
)

#: What a record must name to be auditable: which SDK, which suite
#: outcome, over which tree, produced by what, and when.
ATTESTATION_REQUIRED_FIELDS = (
    "record",
    "sdk_distribution",
    "sdk_version",
    "sdk_types_distribution",
    "sdk_types_version",
    "implements_revision",
    "tests_passed",
    "tests_failed",
    "adapter_digest",
    "recorded_by",
    "recorded_on",
)


def attested_files() -> list[Path]:
    """Every source file the isolated SDK run exercises, in a stable order."""
    found: list[Path] = []
    for tree in ATTESTED_TREES:
        root = PACKAGE_ROOT / tree
        if not root.is_dir():
            continue
        found.extend(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts and "egg-info" not in str(path)
        )
    return sorted(found)


def adapter_digest() -> str:
    """A digest of the sources the SDK leg was run against.

    Hashes ``(relative path, content digest)`` pairs rather than a
    concatenation of the bodies, so moving a file changes the answer as
    surely as editing one does. Paths are package-relative and POSIX, so
    two checkouts of the same commit agree regardless of where they sit
    on disk or which platform they are on.
    """
    digest = hashlib.sha256()
    for path in attested_files():
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        body = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{relative}:{body}\n".encode("utf-8"))
    return digest.hexdigest()


def load_attestation() -> dict[str, Any] | None:
    """The recorded run, or ``None`` when there is none.

    Unreadable is treated the same as absent, for the reason
    :func:`~mcp_heartbeat_conformance.isolation.load_core_size_signoff`
    gives: a record nobody can parse is not evidence of a run, and
    ``None`` re-opens the case instead of letting a malformed file read
    as proof.
    """
    if not SDK_ATTESTATION_PATH.is_file():
        return None
    try:
        record = json.loads(SDK_ATTESTATION_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return record if isinstance(record, dict) else None


def attestation_covers(record: Mapping[str, Any] | None, digest: str) -> bool:
    """Whether ``record`` is a passing run of *this* tree against *the* pin.

    Exact on every clause. A run of a different tree, against a different
    SDK, or with a single failure in it, is not this leg's proof — and
    ``tests_passed > 0`` is there because a suite that collected nothing
    also reports zero failures.
    """
    if not record:
        return False
    if record.get("record") != ATTESTATION_RECORD:
        return False
    if any(record.get(field_name) in (None, "") for field_name in ATTESTATION_REQUIRED_FIELDS):
        return False
    return (
        record.get("adapter_digest") == digest
        and record.get("sdk_distribution") == contract.SDK_PIN.distribution
        and record.get("sdk_version") == contract.SDK_PIN.version
        and record.get("sdk_types_distribution") == contract.SDK_PIN.types_distribution
        and record.get("sdk_types_version") == contract.SDK_PIN.types_version
        and record.get("implements_revision") == contract.PROTOCOL_REVISION
        and record.get("tests_failed") == 0
        and isinstance(record.get("tests_passed"), int)
        and record.get("tests_passed", 0) > 0
    )


__all__ = [
    "ATTESTATION_RECORD",
    "ATTESTATION_REQUIRED_FIELDS",
    "ATTESTED_TREES",
    "SDK_ATTESTATION_PATH",
    "adapter_digest",
    "attestation_covers",
    "attested_files",
    "load_attestation",
]
