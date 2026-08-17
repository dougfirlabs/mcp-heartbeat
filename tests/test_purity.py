"""The standalone seam is a hard constraint, so it gets a test.

The package must build, import, and validate in a clean environment with
no host application, web framework, or MCP SDK. That is easy to
state and easy to break by reflex — one convenience import and the package
stops being liftable.

Three independent proofs:

* the **import graph**, asserted at the AST level, so a forbidden import is
  caught even on a code path no test exercises;
* the **declared dependencies**, asserted against ``pyproject.toml``;
* an actual **clean-room import** in a subprocess started with site-packages
  disabled, which is the only proof that survives "but it works on my
  machine, where the dependency happens to be installed".
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC = PACKAGE_ROOT / "src" / "mcp_heartbeat"

#: Every module in the package. A new file must be added here deliberately.
CORE_MODULES = (
    "__init__.py",
    "clock.py",
    "errors.py",
    "issuer.py",
    "lineage.py",
    "model.py",
    "ports.py",
    "validation.py",
)

#: The modules that make up the *portable core* (story MCP-HB-01-S1). The
#: package/adapter boundary (story S2) is measured separately.
S1_CORE_MODULES = ("clock.py", "errors.py", "issuer.py", "lineage.py", "model.py", "validation.py")

#: Neither MCP era may be imported, and neither may a web framework. The
#: SDK roots are listed even though they are not installed here: the point
#: is that adding one later fails this test rather than passing silently.
FORBIDDEN_ROOTS = (
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "httpx",
    "requests",
    "anyio",
    "jsonschema",
    "mcp",
    "mcp_sdk",
)

#: The PRD's budget for the portable core, in logical lines (blank lines,
#: comments, and docstrings excluded).
PRD_CORE_BUDGET_LOC = 400

#: What the core actually measures today. It is over the PRD budget, which
#: the PRD permits only with an explicit justification — see
#: ``docs/loc-budget.md``. Enforcing the *measured* figure rather than the
#: target means the overrun cannot quietly grow while an operator reviews
#: it. Lower this when the core shrinks; raising it needs a reason in that
#: document.
CORE_CEILING_LOC = 440


def imported_roots(path: Path) -> set[str]:
    """Top-level package names imported absolutely by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — the sanctioned intra-package form.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def logical_loc(path: Path) -> int:
    """Lines of actual implementation: no blanks, comments, or docstrings."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
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


# ── the import graph ──────────────────────────────────────────────────


def test_every_module_is_covered_by_this_test() -> None:
    on_disk = {path.name for path in SRC.glob("*.py")}
    assert on_disk == set(CORE_MODULES), (
        "a new module appeared: add it to CORE_MODULES (and to S1_CORE_MODULES "
        "if it is part of the portable core) deliberately"
    )


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_module_imports_ot_goat_scholia_a_web_framework_or_an_mcp_sdk(
    module: str,
) -> None:
    offending = imported_roots(SRC / module) & set(FORBIDDEN_ROOTS)
    assert offending == set(), f"{module} imports {sorted(offending)}"


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_module_imports_anything_outside_the_standard_library(module: str) -> None:
    third_party = {
        root
        for root in imported_roots(SRC / module)
        if root not in sys.stdlib_module_names and not root.startswith("_")
    }
    assert third_party == set(), f"{module} imports non-stdlib {sorted(third_party)}"


@pytest.mark.parametrize("module", CORE_MODULES)
def test_intra_package_imports_are_relative(module: str) -> None:
    # An absolute self-import breaks the moment the directory is lifted out
    # of this repository, which is the whole point of the seam. Asserted over
    # the import graph rather than the text, so the usage examples in the
    # docstrings — which are written from a *consumer's* point of view and do
    # say `from mcp_heartbeat import ...` — do not trip it.
    assert "mcp_heartbeat" not in imported_roots(SRC / module)


def test_the_package_declares_no_runtime_dependencies() -> None:
    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in manifest


# ── an actual clean-room import ───────────────────────────────────────


CLEAN_ROOM_PROBE = """
import sys
import mcp_heartbeat as hb

forbidden = {roots}
leaked = sorted(forbidden & {{name.split('.')[0] for name in sys.modules}})
assert not leaked, f"clean room leaked {{leaked}}"

clock = hb.FakeClock()
issuer = hb.HeartbeatIssuer(participant_id="clean/room-1", epoch_id="e1", clock=clock)
state = hb.LineageState(participant_id="clean/room-1")

outcome = hb.admit(state, issuer.issue().to_dict(), clock.now())
assert outcome.accepted, outcome.reason
clock.advance(1)
outcome = hb.admit(outcome.state, issuer.issue().to_dict(), clock.now())
assert outcome.accepted, outcome.reason
assert outcome.state.held.sequence == 1
print("clean-room-ok")
"""


def test_the_reference_flow_runs_with_site_packages_disabled() -> None:
    # `-S` skips site.py, so nothing installed into the environment — very
    # much including an editable install — is importable. The only
    # thing on the path is this package's `src` and the standard library.
    probe = CLEAN_ROOM_PROBE.format(roots=repr(set(FORBIDDEN_ROOTS)))
    result = subprocess.run(
        [sys.executable, "-S", "-s", "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(PACKAGE_ROOT),
        env={"PYTHONPATH": str(PACKAGE_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "clean-room-ok" in result.stdout


def test_site_packages_are_genuinely_absent_from_the_clean_room() -> None:
    # Guards the guard: if `-S` ever stopped isolating, the test above would
    # keep passing for the wrong reason.
    result = subprocess.run(
        [sys.executable, "-S", "-s", "-c", "import pytest"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(PACKAGE_ROOT),
        env={"PYTHONPATH": str(PACKAGE_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr


# ── the size budget ───────────────────────────────────────────────────


def test_the_portable_core_stays_within_its_recorded_ceiling() -> None:
    measured = sum(logical_loc(SRC / module) for module in S1_CORE_MODULES)
    assert measured <= CORE_CEILING_LOC, (
        f"portable core grew to {measured} logical LOC (ceiling {CORE_CEILING_LOC}). "
        "Shrink it or justify the expansion in docs/loc-budget.md."
    )


def test_an_overrun_of_the_prd_budget_is_documented() -> None:
    # The PRD says: exceed 400 and you stop and justify. This asserts the
    # justification exists and names the real number, so the deviation
    # cannot survive as an unremarked comment.
    measured = sum(logical_loc(SRC / module) for module in S1_CORE_MODULES)
    if measured <= PRD_CORE_BUDGET_LOC:
        return
    budget_doc = PACKAGE_ROOT / "docs" / "loc-budget.md"
    assert budget_doc.exists(), "core is over the PRD budget and undocumented"
    assert str(measured) in budget_doc.read_text(encoding="utf-8"), (
        f"docs/loc-budget.md must record the current measurement ({measured})"
    )
