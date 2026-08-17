"""Provenance and interoperability for the independent participant.

The hard constraint is that at least one participant must be a
clean-room implementation "authored only from the normative prose,
schema, and fixtures", and that it "must not import, link, copy, or
share implementation logic with the reference core". A sentence in a
README does not establish that, so this module checks all four verbs
mechanically:

**import** — the clean-room module's AST is walked for any import of a
reference package, so a forbidden import is caught even on a code path
no test exercises.

**link** — the participant is imported in a subprocess with
site-packages disabled and *only* the clean-room directory on the path.
If it secretly needed the reference core, it would fail to import.

**copy** — the longest run of *consecutive* shared source lines is
measured, not the count of shared lines. Requiring zero overlap does not
work: two implementations transcribing one schema both write
``for name in REQUIRED_FIELDS:``, because the contract names that field.
Contiguity is what separates a copied block from convergent
transcription, and import statements are excluded on principle — the
constraint forbids shared *logic*, and Python has one spelling for each
stdlib import.

**share logic** — the two implementations are run against the same
inputs and asserted to agree. Agreement is the *point* of the exercise,
so this is the one dimension where a difference is a finding about the
contract rather than a violation.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

from .verdicts import Case, MatrixReport, run_cases

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CLEANROOM_ROOT = PACKAGE_ROOT / "cleanroom"
CLEANROOM_SRC = CLEANROOM_ROOT / "hb_cleanroom"
REFERENCE_SRC = PACKAGE_ROOT / "src" / "mcp_heartbeat"
FIXTURE_ROOT = PACKAGE_ROOT / "tests" / "fixtures"

#: Packages the clean-room implementation may never reach for.
FORBIDDEN_ROOTS: frozenset[str] = frozenset(
    {"mcp_heartbeat", "mcp_heartbeat_legacy", "mcp_heartbeat_current", "mcp_heartbeat_conformance"}
)

#: Shortest normalised source line considered at all. Below this every
#: line is boilerplate two independent authors write identically.
MIN_SHARED_LINE_CHARS = 24

#: Longest run of *consecutive* shared lines that is still credible as
#: coincidence. Four in a row is a copied block, not a convergence.
MAX_SHARED_RUN_LINES = 4

#: Ceiling on scattered overlap. Well above what transcribing one schema
#: produces, well below what copying a module would.
MAX_SHARED_LINE_RATIO = 0.25


def relative(path: Path) -> str:
    """A repo-relative path, for evidence.

    Absolute paths carry the host's home directory, which the PRD
    forbids the pack from containing. Every path that reaches a check
    detail or an observation goes through here; the leak scan that
    ``tools/emit_hb05_evidence.py --check`` runs over the *generated
    pack* is what catches a call site that forgets.
    """
    try:
        return str(path.relative_to(PACKAGE_ROOT))
    except ValueError:
        return path.name


def _imported_roots(path: Path) -> set[str]:
    """Top-level package names imported absolutely by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _significant_lines(path: Path) -> list[str]:
    """Normalised code lines long enough to be evidence, in source order.

    Order is kept because the metric that matters is the longest
    *contiguous* shared run, not the count of shared lines. Two authors
    transcribing one schema will inevitably both write
    ``for name in REQUIRED_FIELDS:``; only a copy produces four such
    lines in a row.
    """
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("import ", "from ")):
            # Import statements are excluded on principle, not by
            # allowlist. The constraint forbids shared *implementation
            # logic*; an import block is a declaration of which stdlib
            # modules the file needs, and Python offers exactly one
            # spelling for each. Two independent authors who both need
            # `datetime` write the same line, in the same place, every
            # time — counting that as evidence of copying would make the
            # metric fire on every honest clean-room implementation.
            continue
        collapsed = " ".join(stripped.split())
        if len(collapsed) >= MIN_SHARED_LINE_CHARS:
            lines.append(collapsed)
    return lines


def longest_shared_run(candidate: list[str], reference: set[str]) -> tuple[int, list[str]]:
    """The longest unbroken stretch of ``candidate`` present in ``reference``."""
    best: list[str] = []
    current: list[str] = []
    for line in candidate:
        if line in reference:
            current.append(line)
            if len(current) > len(best):
                best = list(current)
        else:
            current = []
    return len(best), best


# ── the cases ─────────────────────────────────────────────────────────


def imports_nothing_from_the_reference(case: Case) -> None:
    """No module in the clean room imports a reference package."""
    modules = sorted(CLEANROOM_SRC.glob("*.py"))
    case.check("the_clean_room_has_modules_to_check", modules != [], [p.name for p in modules])

    offences: dict[str, list[str]] = {}
    third_party: dict[str, list[str]] = {}
    for module in modules:
        roots = _imported_roots(module)
        forbidden = sorted(roots & FORBIDDEN_ROOTS)
        if forbidden:
            offences[module.name] = forbidden
        outside = sorted(
            root
            for root in roots
            if root not in sys.stdlib_module_names
            and not root.startswith("_")
            and root != "hb_cleanroom"
        )
        if outside:
            third_party[module.name] = outside

    case.check("imports_no_reference_package", offences == {}, offences)
    case.check("imports_nothing_outside_the_standard_library", third_party == {}, third_party)
    case.observations = {"modules": [p.name for p in modules]}


def lives_outside_the_reference_import_path(case: Case) -> None:
    """The clean room is not even reachable as a sibling of the core."""
    case.check("clean_room_directory_exists", CLEANROOM_SRC.is_dir(), relative(CLEANROOM_SRC))
    case.check(
        "it_is_not_under_the_packages_src_tree",
        (PACKAGE_ROOT / "src") not in CLEANROOM_SRC.parents,
        relative(CLEANROOM_SRC),
    )
    case.check(
        "and_the_reference_core_is_not_under_the_clean_room",
        CLEANROOM_ROOT not in REFERENCE_SRC.parents,
        relative(REFERENCE_SRC),
    )
    case.observations = {"clean_room": relative(CLEANROOM_SRC), "reference": relative(REFERENCE_SRC)}


def runs_without_the_reference_on_the_path(case: Case) -> None:
    """The "link" check: it works with only the clean room importable.

    ``-S`` skips ``site.py``, so nothing installed in the environment —
    including this repository's editable install — can be imported. If
    the participant secretly depended on the reference core, this fails.
    """
    probe = """
import sys
import hb_cleanroom as cr

leaked = sorted({name.split('.')[0] for name in sys.modules} & %(forbidden)s)
assert not leaked, f"clean room leaked {leaked}"

from datetime import datetime, timezone
now = datetime(2026, 1, 1, tzinfo=timezone.utc)
issuer = cr.CleanRoomIssuer(node_id="clean/room-1", boot_id="epoch-1")
consumer = cr.CleanRoomConsumer(node_id="clean/room-1")
assert consumer.admit(issuer.issue(now), now) == "ok"
assert consumer.admit(issuer.issue(now), now) == "ok"
assert consumer.held["sequence"] == 1
print("clean-room-ok")
""" % {"forbidden": repr(set(FORBIDDEN_ROOTS))}

    result = subprocess.run(
        [sys.executable, "-S", "-s", "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(CLEANROOM_ROOT),
        env={"PYTHONPATH": str(CLEANROOM_ROOT), "PATH": "/usr/bin:/bin"},
    )
        # stderr is deliberately not recorded: a traceback names absolute
    # paths inside the runner's home directory, and the pack must carry
    # no private host identifiers. The exit status is the verdict.
    case.check("imported_and_ran_in_isolation", result.returncode == 0, result.returncode)
    case.check("the_reference_flow_completed", "clean-room-ok" in result.stdout, result.stdout)

    # Guards the guard: if `-S` stopped isolating, the check above would
    # keep passing for the wrong reason.
    leak = subprocess.run(
        [sys.executable, "-S", "-s", "-c", "import mcp_heartbeat"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(CLEANROOM_ROOT),
        env={"PYTHONPATH": str(CLEANROOM_ROOT), "PATH": "/usr/bin:/bin"},
    )
    case.check(
        "the_reference_core_is_genuinely_absent",
        leak.returncode != 0 and "ModuleNotFoundError" in leak.stderr,
        leak.returncode,
    )
    case.observations = {"stdout": result.stdout.strip()}


def shares_no_source_with_the_reference(case: Case) -> None:
    """The "copy" check, measured as contiguity rather than coincidence.

    Requiring *zero* shared lines does not work and pretending otherwise
    would be worse than not checking: two implementations transcribing
    one schema both write ``for name in REQUIRED_FIELDS:`` and
    ``from datetime import datetime, timedelta, timezone``, because the
    contract names those fields and the language has one spelling for
    that import.

    What distinguishes copying from convergence is *contiguity*. A copied
    block is a run of consecutive shared lines; independent transcription
    of the same vocabulary is scattered singletons. So the assertion is
    on the longest unbroken run, and the scattered total is reported as
    an observation for a reviewer rather than used as a verdict.
    """
    reference: set[str] = set()
    for module in REFERENCE_SRC.glob("*.py"):
        reference |= set(_significant_lines(module))

    per_module: dict[str, dict[str, Any]] = {}
    worst_run = 0
    worst_excerpt: list[str] = []
    clean_total = 0
    shared_total = 0

    for module in sorted(CLEANROOM_SRC.glob("*.py")):
        lines = _significant_lines(module)
        run, excerpt = longest_shared_run(lines, reference)
        shared = [line for line in lines if line in reference]
        clean_total += len(lines)
        shared_total += len(shared)
        per_module[module.name] = {
            "lines": len(lines),
            "shared": len(shared),
            "longest_run": run,
        }
        if run > worst_run:
            worst_run, worst_excerpt = run, excerpt

    case.check(
        "both_trees_were_actually_read",
        bool(clean_total) and bool(reference),
        {"clean": clean_total, "reference": len(reference)},
    )
    case.check(
        "no_contiguous_block_is_shared",
        worst_run < MAX_SHARED_RUN_LINES,
        {"longest_run": worst_run, "ceiling": MAX_SHARED_RUN_LINES, "excerpt": worst_excerpt},
    )
    # A high scattered overlap would not prove copying, but it would mean
    # the contiguity metric is doing all the work, so it is bounded too.
    ratio = shared_total / clean_total if clean_total else 0.0
    case.check(
        "scattered_overlap_stays_incidental",
        ratio < MAX_SHARED_LINE_RATIO,
        {"ratio": round(ratio, 3), "ceiling": MAX_SHARED_LINE_RATIO, "shared": shared_total},
    )
    case.observations = {
        "per_module": per_module,
        "reference_lines": len(reference),
        "shared_ratio": round(ratio, 3),
        "threshold_chars": MIN_SHARED_LINE_CHARS,
    }


def agrees_with_the_reference_on_the_corpus(case: Case) -> None:
    """The interoperability check, run over the shipped fixtures.

    Positive fixtures must validate under both implementations and
    negative fixtures must be refused by both. Disagreement here is a
    finding about the *contract*, not about either implementation, which
    is precisely what an independent reading is for.
    """
    import json

    from mcp_heartbeat.validation import validate_document

    sys.path.insert(0, str(CLEANROOM_ROOT))
    try:
        from hb_cleanroom.participant import validate as cleanroom_validate
    finally:
        sys.path.remove(str(CLEANROOM_ROOT))

    disagreements: list[dict[str, Any]] = []
    wrong_verdicts: list[dict[str, Any]] = []
    counts = {"positive": 0, "negative": 0}

    for polarity in ("positive", "negative"):
        expected_valid = polarity == "positive"
        for path in sorted((FIXTURE_ROOT / polarity).glob("*.json")):
            # Fixtures are envelopes: the document sits under `document`,
            # and a negative also carries the violation set it must produce.
            fixture = json.loads(path.read_text(encoding="utf-8"))
            document = fixture["document"]
            counts[polarity] += 1

            reference = validate_document(document)
            cleanroom = cleanroom_validate(document)
            row = {
                "fixture": f"{polarity}/{path.name}",
                "reference_valid": reference == [],
                "cleanroom_valid": cleanroom == [],
            }
            if (reference == []) != (cleanroom == []):
                disagreements.append(
                    dict(row, reference=reference, cleanroom=cleanroom)
                )
            if (cleanroom == []) is not expected_valid:
                wrong_verdicts.append(dict(row, expected_valid=expected_valid))

    case.check(
        "the_corpus_was_found",
        bool(counts["positive"]) and bool(counts["negative"]),
        counts,
    )
    case.check(
        "the_two_implementations_never_disagree_on_validity",
        disagreements == [],
        disagreements,
    )
    case.check(
        "the_clean_room_gives_the_corpus_its_declared_verdict",
        wrong_verdicts == [],
        wrong_verdicts,
    )
    case.observations = {"fixture_counts": counts, "disagreements": disagreements}


def interoperates_in_both_directions(case: Case) -> None:
    """Each implementation admits the other's documents.

    One direction is not enough: an implementation could emit something
    universally acceptable and accept nothing, and a one-way test would
    call that interoperable.
    """
    from datetime import datetime, timedelta, timezone

    from mcp_heartbeat.clock import FakeClock
    from mcp_heartbeat.issuer import HeartbeatIssuer
    from mcp_heartbeat.lineage import LineageState, admit
    from mcp_heartbeat.validation import validate_document

    sys.path.insert(0, str(CLEANROOM_ROOT))
    try:
        from hb_cleanroom.participant import (
            CleanRoomConsumer,
            CleanRoomIssuer,
            validate as cleanroom_validate,
        )
    finally:
        sys.path.remove(str(CLEANROOM_ROOT))

    participant = "svc/interop-1"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Clean-room issuer → reference consumer.
    cr_issuer = CleanRoomIssuer(node_id=participant, boot_id="epoch-clean")
    state = LineageState(participant_id=participant)
    reference_outcomes = []
    for step in range(3):
        document = cr_issuer.issue(now + timedelta(seconds=step))
        case.check(
            f"reference_validator_accepts_clean_room_document_{step}",
            validate_document(document) == [],
            validate_document(document),
        )
        admission = admit(state, document, now + timedelta(seconds=step))
        state = admission.state
        reference_outcomes.append(admission.reason.value if admission.reason else "ok")
    case.check(
        "reference_consumer_admitted_every_clean_room_beat",
        set(reference_outcomes) == {"ok"},
        reference_outcomes,
    )
    case.check(
        "and_tracked_the_sequence",
        state.held is not None and state.held.sequence == 2,
        state.held.sequence if state.held else None,
    )

    # Reference issuer → clean-room consumer.
    clock = FakeClock(start=now)
    ref_issuer = HeartbeatIssuer(
        participant_id=participant, epoch_id="epoch-ref", clock=clock
    )
    cr_consumer = CleanRoomConsumer(node_id=participant)
    cleanroom_outcomes = []
    for _ in range(3):
        document = ref_issuer.issue().to_dict()
        cleanroom_outcomes.append(cr_consumer.admit(document, clock.now()))
        clock.advance(1.0)
    case.check(
        "clean_room_consumer_admitted_every_reference_beat",
        set(cleanroom_outcomes) == {"ok"},
        cleanroom_outcomes,
    )
    case.check(
        "and_tracked_the_sequence",
        cr_consumer.held is not None and cr_consumer.held["sequence"] == 2,
        cr_consumer.held["sequence"] if cr_consumer.held else None,
    )

    # And the clean-room validator agrees about the reference's output.
    final = ref_issuer.issue().to_dict()
    case.check(
        "clean_room_validator_accepts_reference_documents",
        cleanroom_validate(final) == [],
        cleanroom_validate(final),
    )
    case.observations = {
        "reference_outcomes": reference_outcomes,
        "cleanroom_outcomes": cleanroom_outcomes,
    }


def agrees_on_lineage_refusals(case: Case) -> None:
    """The hard part of the contract: both refuse the same replays.

    Validation agreement is comparatively easy — it is transcribed from
    a schema. Lineage is prose, so it is where two independent readings
    are most likely to diverge, and therefore where agreement is worth
    the most.
    """
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(CLEANROOM_ROOT))
    try:
        from hb_cleanroom.participant import CleanRoomConsumer, CleanRoomIssuer
    finally:
        sys.path.remove(str(CLEANROOM_ROOT))

    from mcp_heartbeat.lineage import LineageState, admit

    participant = "svc/interop-1"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    issuer = CleanRoomIssuer(node_id=participant, boot_id="epoch-a")

    beats = [issuer.issue(now + timedelta(seconds=step)) for step in range(3)]
    issuer.restart("epoch-b")
    new_epoch = issuer.issue(now + timedelta(seconds=4))

    # Replay of an old beat, then a return to the retired epoch.
    trials = [
        ("advance", beats[0], 0),
        ("advance", beats[1], 1),
        ("rollback", beats[0], 2),
        ("new-epoch", new_epoch, 4),
        ("retired-epoch", beats[2], 5),
    ]

    cr_consumer = CleanRoomConsumer(node_id=participant)
    state = LineageState(participant_id=participant)
    rows: list[dict[str, Any]] = []
    for label, document, offset in trials:
        moment = now + timedelta(seconds=offset)
        cleanroom_reason = cr_consumer.admit(document, moment)
        admission = admit(state, document, moment)
        state = admission.state
        if admission.reason is not None:
            reference_reason = admission.reason.value
        elif admission.duplicate:
            reference_reason = "duplicate"
        else:
            reference_reason = "ok"
        rows.append(
            {"trial": label, "reference": reference_reason, "cleanroom": cleanroom_reason}
        )

    disagreements = [row for row in rows if row["reference"] != row["cleanroom"]]
    case.check("every_trial_ran", len(rows) == len(trials), len(rows))
    case.check("both_implementations_gave_the_same_reason", disagreements == [], disagreements)
    case.check(
        "the_rollback_was_refused_by_both",
        rows[2]["reference"] == "sequence_rollback" and rows[2]["cleanroom"] == "sequence_rollback",
        rows[2],
    )
    case.check(
        "the_retired_epoch_was_refused_by_both",
        rows[4]["reference"] == "boot_id_reuse" and rows[4]["cleanroom"] == "boot_id_reuse",
        rows[4],
    )
    case.observations = {"trials": rows}


CASES: tuple[tuple[str, str, Any], ...] = (
    ("no-imports", "The clean room imports no reference package", imports_nothing_from_the_reference),
    ("separate-tree", "The clean room lives off the reference import path", lives_outside_the_reference_import_path),
    ("no-link", "It imports and runs with the reference absent", runs_without_the_reference_on_the_path),
    ("no-copy", "No substantial source line appears in both trees", shares_no_source_with_the_reference),
    ("corpus-agreement", "Both agree on every conformance fixture", agrees_with_the_reference_on_the_corpus),
    ("bidirectional-interop", "Each admits the other's documents", interoperates_in_both_directions),
    ("lineage-agreement", "Both refuse the same replays for the same reason", agrees_on_lineage_refusals),
)


def run() -> MatrixReport:
    """Run the clean-room provenance and interoperability matrix."""
    report = MatrixReport(
        matrix_id="clean-room",
        title="Independent participant provenance and two-way interoperability",
    )
    return run_cases(report, CASES)


__all__ = ["CASES", "CLEANROOM_ROOT", "CLEANROOM_SRC", "FORBIDDEN_ROOTS", "run"]
