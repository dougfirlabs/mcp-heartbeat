"""The closure matrix, and the guards that keep the two corpora apart.

Three obligations from the PRD's hard constraints get tests here rather than
prose:

* every HB-02-owned defect is inverted by the repaired corpus with **both**
  polarities;
* the **historical** characterization tests are unchanged — the repaired
  expectations live beside them, never on top of them;
* the legacy adapter stays on its own side of the boundary: no policy table,
  no transport, and no reverse dependency from the portable core.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest

from mcp_heartbeat_legacy import (
    LEGACY_ONLY_PRIMITIVES,
    MODERN_EXTENSION_ID,
    LegacySessionIdentityBinder,
    advertise,
)

from test_repaired_corpus import CORPUS, VECTORS  # noqa: E402 - sibling test module

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = PACKAGE_ROOT / "src" / "mcp_heartbeat"
ADAPTER_SRC = PACKAGE_ROOT / "src" / "mcp_heartbeat_legacy"

#: The defects the HB-00 register assigns to this PRD.
OWNED_DEFECTS = ("D-02", "D-03", "D-04", "D-10")

#: The archived HB-00 reproducers. They live in the originating
#: repository and are not published with this package, so this path
#: normally does not exist and every assertion that reads it skips
#: rather than fails. Drop the file here to run the closure checks
#: against the real baseline.
HISTORICAL_REPRODUCERS = (
    PACKAGE_ROOT / "tests" / "legacy" / "historical" / "test_hb00_reproducers.py"
)

#: sha256 (first 16 hex) of each HB-02-owned reproducer's source segment at
#: the HB-00 baseline. Hashing per function rather than per file means a later
#: PRD legitimately inverting *its own* defect in the same module does not
#: trip this guard, while any edit to the three below does.
HISTORICAL_DIGESTS = {
    "test_d02_lab_advertises_resources_subscribe_it_does_not_serve": "bd41fcd4f74e1de2",
    "test_d03_initialized_notification_is_neither_sent_nor_handled": "03be7ffeb1322c04",
    "test_d04_lab_server_echoes_any_requested_protocol_version": "1f15eb6d102f35b3",
}


# ── the closure matrix ────────────────────────────────────────────────


def test_the_corpus_closes_exactly_the_defects_this_prd_owns() -> None:
    assert tuple(CORPUS["defects_closed"]) == OWNED_DEFECTS


@pytest.mark.parametrize("defect", OWNED_DEFECTS)
def test_each_owned_defect_has_positive_and_negative_coverage(defect: str) -> None:
    polarities = {
        vector["polarity"] for vector in VECTORS if vector.get("defect") == defect
    }
    assert polarities == {"positive", "negative"}, (
        f"{defect} needs both polarities in the repaired corpus, has {sorted(polarities)}"
    )


def test_the_closure_matrix_is_printable() -> None:
    # The PRD requires a legacy defect closure matrix as evidence. It is
    # computed from the corpus rather than maintained beside it, so it cannot
    # drift from the vectors that justify it.
    matrix = {
        defect: sorted(
            vector["id"] for vector in VECTORS if vector.get("defect") == defect
        )
        for defect in OWNED_DEFECTS
    }
    assert all(matrix[defect] for defect in OWNED_DEFECTS)
    assert json.dumps(matrix)


# ── the historical corpus is untouched ────────────────────────────────


def _historical_source() -> str:
    if not HISTORICAL_REPRODUCERS.exists():
        pytest.skip("historical corpus lives in the originating checkout; not present")
    return HISTORICAL_REPRODUCERS.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(HISTORICAL_DIGESTS))
def test_the_historical_reproducer_is_byte_for_byte_unchanged(name: str) -> None:
    source = _historical_source()
    tree = ast.parse(source)
    functions = {
        node.name: ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert name in functions, f"{name} disappeared from the historical corpus"

    digest = hashlib.sha256((functions[name] or "").encode("utf-8")).hexdigest()[:16]
    assert digest == HISTORICAL_DIGESTS[name], (
        f"{name} was edited. The HB-00 characterization tests are immutable: the "
        "repaired expectations belong in corpus/legacy-repaired-1.json, which is "
        "separately versioned precisely so this file never has to change."
    )


def test_the_two_corpora_are_independently_runnable() -> None:
    # The repaired corpus resolves entirely inside the package...
    assert (Path(__file__).parent / "corpus" / "legacy-repaired-1.json").exists()
    # ...and names the historical one by path without importing it.
    assert CORPUS["historical_corpus"]["immutable"] is True
    assert CORPUS["historical_corpus"]["reproducers"].endswith(
        "test_hb00_reproducers.py"
    )


def test_d10_still_has_no_historical_reproducer() -> None:
    """A gap in the HB-00 pack, recorded rather than quietly worked around.

    The register says every defect has a reproducer, but D-10 has none — it
    is the one defect the archived module describes only in prose. This
    adapter therefore has no historical behaviour to invert for D-10, only the
    repaired corpus's positive/negative vectors. If someone later adds a D-10
    reproducer, this fails and the repaired corpus must be cross-checked
    against it.
    """
    source = _historical_source()
    names = [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
    ]
    assert not [name for name in names if name.startswith("test_d10")]


# ── the adapter stays on its own side of the boundary ─────────────────


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


ADAPTER_MODULES = sorted(path.name for path in ADAPTER_SRC.glob("*.py"))

#: The adapter may import the core. It may not import a host application, a web
#: framework, or an MCP SDK: the legacy contract is expressed as method names
#: and dictionaries, so nothing here needs a transport.
FORBIDDEN_ROOTS = frozenset(
    {
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
    }
)


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_the_adapter_imports_no_transport_sdk_or_host_application(module: str) -> None:
    offending = imported_roots(ADAPTER_SRC / module) & FORBIDDEN_ROOTS
    assert offending == set(), f"{module} imports {sorted(offending)}"


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_the_adapter_imports_nothing_third_party(module: str) -> None:
    third_party = {
        root
        for root in imported_roots(ADAPTER_SRC / module)
        if root not in sys.stdlib_module_names
        and not root.startswith("_")
        and root != "mcp_heartbeat"
    }
    assert third_party == set(), f"{module} imports non-stdlib {sorted(third_party)}"


@pytest.mark.parametrize("module", sorted(path.name for path in CORE_SRC.glob("*.py")))
def test_the_portable_core_never_imports_the_legacy_adapter(module: str) -> None:
    # The dependency runs one way. A core that knew about a legacy adapter
    # would carry an era, which is the coupling the whole epic is undoing.
    assert "mcp_heartbeat_legacy" not in imported_roots(CORE_SRC / module)


def test_the_adapter_owns_no_principal_mapping() -> None:
    # D-N3: the deployment owner injects the mapping. A default would make
    # this package the policy owner by accident.
    permitted = inspect.signature(LegacySessionIdentityBinder.__init__).parameters[
        "permitted"
    ]
    assert permitted.default is inspect.Parameter.empty
    assert permitted.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_adapter_never_advertises_the_modern_identifier() -> None:
    for registry in ((), ("resources/read",), ("resources/read", "resources/subscribe")):
        advertised = advertise(registry)
        assert MODERN_EXTENSION_ID not in json.dumps(advertised)


def test_the_adapter_ships_without_widening_the_distribution_s_dependency() -> None:
    # The adapter *is* part of the distribution — one wheel carries the
    # core and both eras. What must not widen is the dependency: this
    # adapter is standard library plus the core (asserted module by
    # module above), so shipping it costs an installer nothing and the
    # core's clean-environment proof is untouched.
    #
    # This replaces an assertion that the adapter was excluded. That
    # exclusion was one way to protect the clean-environment guarantee,
    # not the guarantee itself; `tests/test_packaging.py` now proves the
    # guarantee directly, against a built and installed wheel.
    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp_heartbeat_legacy"' in manifest
    assert '"mcp_heartbeat_legacy.*"' in manifest
    assert "dependencies = []" in manifest


def test_the_legacy_only_primitives_are_named() -> None:
    # Named so HB-03 has an explicit list to keep off the modern path,
    # rather than rediscovering it from the archived lab.
    assert {
        "initialize",
        "notifications/initialized",
        "resources/subscribe",
        "notifications/resources/updated",
    } <= LEGACY_ONLY_PRIMITIVES
