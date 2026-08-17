"""Package boundaries, core size, and the security/privacy/IP scans.

The project-separation contract is a layered one, and the layering is
the whole point — each package may depend inwards and never outwards:

    mcp_heartbeat              (portable core: stdlib only)
      ↑
    mcp_heartbeat_legacy       may import the core
    mcp_heartbeat_current      may import the core
      ↑
    mcp_heartbeat_conformance  may import all three
      ↑
    host application           may import all of the above; none may
                               import it

:data:`ALLOWED_INTERNAL_IMPORTS` states that as data, and
:func:`package_boundaries_hold` walks every module's AST against it. An
AST check rather than a text one, because a forbidden import must be
caught on a code path no test exercises.

**On the IP scan and what it does not hardcode.** This package is meant
to be liftable into a public repository, so it must not carry any
project's list of confidential names — writing them here to grep for them
would put them in the very artifact the scan exists to keep them out
of. :func:`scan_for_confidential_terms` therefore takes the denied list
as a parameter, defaulting to empty, and the *structural* leaks that can
be detected without a vocabulary — absolute home paths, email addresses,
private host identifiers, credential-shaped assignments — are checked
unconditionally. The publishing project supplies its own term
list from its own side, from a module that is never copied into this
tree.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from .verdicts import Case, MatrixReport, run_cases

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PACKAGE_ROOT / "src"
CLEANROOM_ROOT = PACKAGE_ROOT / "cleanroom"

#: This module, excluded from its own tree scan — it owns the patterns
#: and holds a synthetic sample of each. See `the_tree_leaks_nothing_structural`.
SCANNER_MODULE = Path(__file__).resolve()

#: Every package in the tree, and which siblings each may import. The
#: core's empty set is the load-bearing entry: it is what makes the
#: package liftable.
ALLOWED_INTERNAL_IMPORTS: Mapping[str, frozenset[str]] = {
    "mcp_heartbeat": frozenset(),
    "mcp_heartbeat_legacy": frozenset({"mcp_heartbeat"}),
    "mcp_heartbeat_current": frozenset({"mcp_heartbeat"}),
    "mcp_heartbeat_conformance": frozenset(
        {"mcp_heartbeat", "mcp_heartbeat_legacy", "mcp_heartbeat_current"}
    ),
}

#: Roots nothing in this tree may import, in any package. ``mcp`` and
#: ``mcp_sdk`` are listed with an exception for the current adapter's
#: single ``sdk`` shim, which exists precisely to quarantine them.
FORBIDDEN_ROOTS: frozenset[str] = frozenset(
    {"fastapi", "starlette", "uvicorn", "django", "flask"}
)

#: The only module permitted to import the official SDK. Everything else
#: talks to the SDK through it, so an absent SDK degrades one import.
SDK_QUARANTINE = ("mcp_heartbeat_current", "sdk.py")
SDK_ROOTS: frozenset[str] = frozenset({"mcp", "mcp_sdk", "mcp_types"})

#: The portable core's recorded ceiling, mirroring ``tests/test_purity.py``.
CORE_CEILING_LOC = 440
PRD_CORE_BUDGET_LOC = 400
S1_CORE_MODULES = ("clock.py", "errors.py", "issuer.py", "lineage.py", "model.py", "validation.py")

#: The operator's decision on the overrun, as data rather than prose.
#:
#: The measurement above is deliberately untouched by that decision: the
#: core still measures what it measures, the budget is still 400, and the
#: overrun is still 30. What the signoff changes is only whether the
#: overrun is *accepted*. Keeping it in its own file, keyed to the exact
#: number it accepts, is what stops "sign off the overrun" from becoming
#: "raise the budget until it fits" — a signoff for 430 says nothing
#: about 431, and :func:`the_core_stays_within_its_budget` re-opens the
#: HOLD rather than inheriting the approval.
CORE_SIZE_SIGNOFF_PATH = PACKAGE_ROOT / "docs" / "core-size-signoff.json"

#: What a signoff must name to be auditable: which number was accepted,
#: against which budget, by whom, when, and why.
SIGNOFF_REQUIRED_FIELDS = (
    "decision",
    "measured_loc",
    "prd_budget_loc",
    "approver",
    "approved_on",
    "rationale",
)

# Structural leak patterns. Each one is a shape, not a vocabulary, so
# they work without knowing any confidential term.
LEAK_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "home_directory_path": re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+"),
    "email_address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "private_ipv4": re.compile(r"\b(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|100\.\d+\.)\d+\.\d+\b"),
    "tailnet_hostname": re.compile(r"\b[A-Za-z0-9-]+\.ts\.net\b"),
    "bearer_token": re.compile(r"\b(?:sk|pk|ghp|gho|xox[bap])[-_][A-Za-z0-9]{16,}"),
    "assigned_secret": re.compile(
        r"(?i)\b(?:api_?key|secret|password|passwd|token|credential)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
    ),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def relative(path: Path) -> str:
    """A repo-relative path, for evidence. See the note in `.cleanroom`."""
    try:
        return str(path.relative_to(PACKAGE_ROOT))
    except ValueError:
        return path.name


def python_files(root: Path) -> list[Path]:
    """Every ``.py`` under ``root``, excluding caches and build output."""
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and "egg-info" not in str(path)
    )


def imported_roots(path: Path) -> set[str]:
    """Top-level package names imported absolutely by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def logical_loc(path: Path) -> int:
    """Lines of implementation: no blanks, comments, or docstrings."""
    import tokenize

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.update(range(body[0].lineno, (body[0].end_lineno or 0) + 1))

    with path.open(encoding="utf-8") as handle:
        comments = {
            token.start[0]
            for token in tokenize.generate_tokens(handle.readline)
            if token.type == tokenize.COMMENT
        }

    count = 0
    for number, line in enumerate(source.splitlines(), start=1):
        if not line.strip() or number in docstrings:
            continue
        if number in comments and not line.split("#")[0].strip():
            continue
        count += 1
    return count


def load_core_size_signoff() -> dict[str, Any] | None:
    """The operator's overrun signoff, or ``None`` when there is none.

    Unreadable is treated the same as absent. A signoff nobody can parse
    is not evidence of a decision, and returning ``None`` re-opens the
    HOLD instead of letting a malformed file read as approval.
    """
    import json

    if not CORE_SIZE_SIGNOFF_PATH.is_file():
        return None
    try:
        record = json.loads(CORE_SIZE_SIGNOFF_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return record if isinstance(record, dict) else None


def signoff_covers(record: Mapping[str, Any] | None, measured: int) -> bool:
    """Whether ``record`` accepts *this* measurement against *this* budget.

    Deliberately exact on both numbers. A signoff is approval of one
    measured figure, not a standing waiver of the budget, so it must not
    survive a change to either side of the comparison it approved.
    """
    if not record:
        return False
    return (
        record.get("decision") == "accepted"
        and record.get("measured_loc") == measured
        and record.get("prd_budget_loc") == PRD_CORE_BUDGET_LOC
        and all(record.get(field_name) for field_name in SIGNOFF_REQUIRED_FIELDS)
    )


def scan_for_leaks(text: str) -> dict[str, list[str]]:
    """Every structural leak pattern that matched, with its matches."""
    found: dict[str, list[str]] = {}
    for name, pattern in LEAK_PATTERNS.items():
        matches = sorted(set(pattern.findall(text)))
        if matches:
            found[name] = matches[:5]
    return found


def scan_for_confidential_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Which of ``terms`` appear in ``text``, case-insensitively.

    The caller supplies the vocabulary. This package deliberately ships
    none: embedding a confidential term to grep for it would leak it
    into the artifact the scan protects.
    """
    lowered = text.lower()
    return sorted({term for term in terms if term and term.lower() in lowered})


# ── the cases ─────────────────────────────────────────────────────────


def package_boundaries_hold(case: Case) -> None:
    """Each package imports inwards only, and never a host application."""
    violations: list[dict[str, Any]] = []
    sdk_importers: list[str] = []
    checked = 0

    for package, allowed in ALLOWED_INTERNAL_IMPORTS.items():
        package_root = SRC_ROOT / package
        if not package_root.is_dir():
            violations.append({"package": package, "problem": "missing from src/"})
            continue
        for module in python_files(package_root):
            checked += 1
            roots = imported_roots(module)
            relative = f"{package}/{module.name}"

            forbidden = sorted(roots & FORBIDDEN_ROOTS)
            if forbidden:
                violations.append({"module": relative, "imports": forbidden, "rule": "forbidden root"})

            siblings = {r for r in roots if r.startswith("mcp_heartbeat")} - {package}
            outward = sorted(siblings - allowed)
            if outward:
                violations.append({"module": relative, "imports": outward, "rule": "outward import"})

            if roots & SDK_ROOTS:
                sdk_importers.append(relative)

    case.check("every_package_was_found_and_checked", checked > 0, checked)
    case.check("no_package_imports_outwards_or_into_a_host_application", violations == [], violations)
    case.check(
        "only_the_quarantine_module_imports_the_official_sdk",
        sdk_importers == [f"{SDK_QUARANTINE[0]}/{SDK_QUARANTINE[1]}"],
        sdk_importers,
    )
    case.observations = {
        "modules_checked": checked,
        "layering": {k: sorted(v) for k, v in ALLOWED_INTERNAL_IMPORTS.items()},
        "sdk_importers": sdk_importers,
    }


def the_core_uses_only_the_standard_library(case: Case) -> None:
    """The portable core must be liftable into an empty environment."""
    core = SRC_ROOT / "mcp_heartbeat"
    offenders: dict[str, list[str]] = {}
    for module in python_files(core):
        third_party = sorted(
            root
            for root in imported_roots(module)
            if root not in sys.stdlib_module_names and not root.startswith("_")
        )
        if third_party:
            offenders[module.name] = third_party

    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    case.check("the_core_imports_only_the_standard_library", offenders == {}, offenders)
    case.check("the_manifest_declares_no_runtime_dependencies", "dependencies = []" in manifest)
    case.check(
        "and_the_core_does_not_self_import_absolutely",
        all("mcp_heartbeat" not in imported_roots(m) for m in python_files(core)),
    )
    case.observations = {"modules": [m.name for m in python_files(core)]}


def the_core_stays_within_its_budget(case: Case) -> None:
    """Core size against the recorded ceiling, the PRD budget, and the signoff.

    The overrun clears this case only by being **accepted**, never by
    being made to disappear. The measurement below is the same one the
    case has always reported, and the budget it is compared against is
    unchanged; the only new input is
    :func:`load_core_size_signoff`, and it is checked against the exact
    figure it approved. So an operator can accept 430, and the case still
    re-opens on 431 — which is the difference between signing off an
    overrun and quietly raising the budget.
    """
    core = SRC_ROOT / "mcp_heartbeat"
    per_module = {name: logical_loc(core / name) for name in S1_CORE_MODULES}
    measured = sum(per_module.values())
    budget_doc = PACKAGE_ROOT / "docs" / "loc-budget.md"
    signoff = load_core_size_signoff()

    case.check("every_core_module_was_measured", len(per_module) == len(S1_CORE_MODULES))
    case.observations = {
        "per_module": per_module,
        "measured": measured,
        "recorded_ceiling": CORE_CEILING_LOC,
        "prd_budget": PRD_CORE_BUDGET_LOC,
        "signoff": signoff,
        "signoff_covers_this_measurement": signoff_covers(signoff, measured),
    }

    if measured > CORE_CEILING_LOC:
        case.hold(
            f"the portable core measures {measured} logical LOC, over its recorded "
            f"ceiling of {CORE_CEILING_LOC}"
        )
        return

    if measured > PRD_CORE_BUDGET_LOC:
        # Over the PRD budget but inside the recorded ceiling: permitted
        # only while the justification exists and names the real number.
        case.check("the_overrun_is_documented", budget_doc.is_file(), relative(budget_doc))
        case.check(
            "and_the_document_names_the_current_measurement",
            budget_doc.is_file() and str(measured) in budget_doc.read_text(encoding="utf-8"),
            measured,
        )
        if not case.passed:
            return

        if signoff is None or not signoff_covers(signoff, measured):
            # No signoff, or one written for a different number. Either
            # way the overrun is unaccepted, and that is an operator's
            # call rather than a defect — HOLD, not FAIL.
            case.hold(
                f"the core measures {measured} logical LOC against the PRD's "
                f"{PRD_CORE_BUDGET_LOC} budget; the documented justification in "
                f"docs/loc-budget.md needs operator sign-off"
            )
            return

        case.check(
            "an_operator_accepted_this_exact_overrun",
            signoff.get("measured_loc") == measured,
            {"signed_off": signoff.get("measured_loc"), "measured": measured},
        )
        case.check(
            "against_the_unchanged_prd_budget",
            signoff.get("prd_budget_loc") == PRD_CORE_BUDGET_LOC,
            signoff.get("prd_budget_loc"),
        )
        case.check(
            "the_signoff_names_an_approver_a_date_and_a_reason",
            all(signoff.get(field_name) for field_name in SIGNOFF_REQUIRED_FIELDS),
            sorted(f for f in SIGNOFF_REQUIRED_FIELDS if not signoff.get(f)),
        )
        case.check(
            "and_the_measurement_itself_was_not_moved_to_fit",
            measured > PRD_CORE_BUDGET_LOC and measured <= CORE_CEILING_LOC,
            {"measured": measured, "budget": PRD_CORE_BUDGET_LOC, "ceiling": CORE_CEILING_LOC},
        )
        return

    case.check("within_the_prd_budget", measured <= PRD_CORE_BUDGET_LOC, measured)


def the_tree_leaks_nothing_structural(case: Case) -> None:
    """No source file carries a host path, credential, or private address."""
    leaks: dict[str, dict[str, list[str]]] = {}
    scanned = 0
    for root in (SRC_ROOT, CLEANROOM_ROOT):
        if not root.is_dir():
            continue
        for path in python_files(root):
            if path.resolve() == SCANNER_MODULE:
                # This module necessarily contains text matching every
                # pattern — it defines them, and `scanner-works` proves
                # they fire by holding a synthetic sample of each. The
                # samples use RFC 5737 / RFC 2606 reserved values, so
                # excluding exactly one file (by resolved path, not by
                # name) costs no real coverage. `scanner-works` is what
                # stops this exclusion from hiding a broken scanner.
                continue
            scanned += 1
            found = scan_for_leaks(path.read_text(encoding="utf-8"))
            if found:
                leaks[str(path.relative_to(PACKAGE_ROOT))] = found

    case.check("files_were_scanned", scanned > 0, scanned)
    case.check("no_structural_leak_was_found", leaks == {}, leaks)
    case.check(
        "exactly_one_file_was_excluded_and_it_is_the_scanner",
        SCANNER_MODULE.name == "isolation.py" and SCANNER_MODULE.is_file(),
        relative(SCANNER_MODULE),
    )
    case.observations = {
        "files_scanned": scanned,
        "patterns": sorted(LEAK_PATTERNS),
        "excluded": [relative(SCANNER_MODULE)],
    }


def the_scanner_actually_detects_a_leak(case: Case) -> None:
    """Guards the guard: a scanner that matches nothing proves nothing."""
    samples = {
        "home_directory_path": "config lives at /home/example/.config/app.toml",
        "email_address": "contact someone@example.com for access",
        "private_ipv4": "the host answers on 192.168.1.10",
        "tailnet_hostname": "reachable at example-host.ts.net",
        "bearer_token": "token = sk-ABCDEFGHIJKLMNOPQRSTUV",
        "assigned_secret": 'api_key = "hunter2hunter2"',
        # Assembled rather than spelled. A repo-wide secret scan that
        # greps every tracked file for the literal PEM banners would
        # flag this fixture, and a scanner that tripped one security
        # check with its own test data would be asking that check to
        # fail so this one could pass.
        "private_key_block": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    }
    missed = [name for name, text in samples.items() if name not in scan_for_leaks(text)]
    case.check("every_pattern_matches_its_own_sample", missed == [], missed)
    case.check("and_clean_text_matches_nothing", scan_for_leaks("a heartbeat is a lease") == {})
    case.check(
        "the_confidential_term_scan_is_vocabulary_free_by_default",
        scan_for_confidential_terms("anything at all", []) == [],
    )
    case.check(
        "but_finds_a_term_when_one_is_supplied",
        scan_for_confidential_terms("the Rivendell programme", ["rivendell"]) == ["rivendell"],
    )
    case.observations = {"patterns_verified": sorted(samples)}


def the_clean_room_is_not_shipped_as_part_of_the_package(case: Case) -> None:
    """The independent participant must not become a package dependency.

    If ``cleanroom/`` were inside ``src/`` it would be installed
    alongside the core, and the "independent" implementation would ship
    as part of the thing it is meant to independently check.
    """
    case.check("the_clean_room_exists", CLEANROOM_ROOT.is_dir(), relative(CLEANROOM_ROOT))
    case.check(
        "it_is_not_under_src", SRC_ROOT not in CLEANROOM_ROOT.parents, relative(CLEANROOM_ROOT)
    )

    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    case.check(
        "and_it_is_not_declared_as_a_distributed_package",
        "hb_cleanroom" not in manifest,
        "hb_cleanroom appears in pyproject.toml",
    )
    case.check(
        "it_carries_its_own_provenance_statement",
        (CLEANROOM_ROOT / "PROVENANCE.md").is_file(),
    )
    case.observations = {"clean_room": relative(CLEANROOM_ROOT)}


CASES: tuple[tuple[str, str, Any], ...] = (
    ("boundaries", "Each package imports inwards only", package_boundaries_hold),
    ("stdlib-only", "The portable core needs only the standard library", the_core_uses_only_the_standard_library),
    ("core-size", "Core size against ceiling and PRD budget", the_core_stays_within_its_budget),
    ("no-leaks", "No source file carries a host path or credential", the_tree_leaks_nothing_structural),
    ("scanner-works", "The leak scanner detects its own samples", the_scanner_actually_detects_a_leak),
    ("cleanroom-not-shipped", "The independent participant is not a package dependency", the_clean_room_is_not_shipped_as_part_of_the_package),
)


def run() -> MatrixReport:
    """Run the package-isolation, size, and scan matrix."""
    report = MatrixReport(
        matrix_id="isolation",
        title="Package boundaries, portable-core size, and security/privacy/IP scans",
    )
    return run_cases(report, CASES)


__all__ = [
    "ALLOWED_INTERNAL_IMPORTS",
    "CASES",
    "CORE_CEILING_LOC",
    "CORE_SIZE_SIGNOFF_PATH",
    "FORBIDDEN_ROOTS",
    "LEAK_PATTERNS",
    "PRD_CORE_BUDGET_LOC",
    "SIGNOFF_REQUIRED_FIELDS",
    "load_core_size_signoff",
    "logical_loc",
    "run",
    "scan_for_confidential_terms",
    "scan_for_leaks",
    "signoff_covers",
]
