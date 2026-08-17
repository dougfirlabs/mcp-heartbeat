"""What the single wheel ships, and what shipping it must not cost.

The distribution is one wheel carrying the portable core and both era
adapters. Widening the wheel is only safe while the invariants that made
a narrow wheel attractive survive it, so those invariants get a test.

Two kinds of assertion, and the difference matters:

* a handful of **manifest** checks, which prove the intent is written
  down in ``pyproject.toml``;
* the **artifact** checks, which build the wheel and the sdist, install
  the wheel into a throwaway virtualenv, and probe it there.

Only the second kind proves anything. A manifest is a statement about
what should happen; a wheel is what happened. The manifest checks are
here because they fail with a one-line diff when someone edits the
config, which is a friendlier first failure than a build — not because
they are evidence. ``tools/verify_wheel.py`` owns the real proof and is
runnable on its own; this module runs it and reports each of its checks
as a test, so a regression names the invariant it broke.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
LANE = PACKAGE_ROOT / "tools" / "verify_wheel.py"

#: Building and installing is slower than the rest of this suite by an
#: order of magnitude, and the package's default timeout is tuned for
#: pure-Python tests.
pytestmark = pytest.mark.timeout(300)


def load_lane():
    """Import ``tools/verify_wheel.py`` as a module.

    By path, because ``tools/`` is not a package and deliberately does
    not ship — importing it normally would mean putting the tooling
    somewhere the wheel could reach, which is the thing under test.
    """
    spec = importlib.util.spec_from_file_location("hb_verify_wheel", LANE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `@dataclass` resolves string
    # annotations through `sys.modules[cls.__module__]`, and a module
    # that is not there yet makes the decorator fail on import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: Every check the lane must report, in order. Listed explicitly so a
#: probe that silently stops running fails here rather than passing by
#: producing nothing. A new check is added deliberately.
REQUIRED_CHECKS = (
    # the build itself
    "the_package_builds_one_wheel_and_one_sdist",
    # what the artifacts contain (story HB-X2-S3)
    "the_wheel_ships_all_three_packages",
    "and_withholds_the_conformance_tooling_and_clean_room",
    "no_dependency_is_unconditional_in_the_built_metadata",
    "the_current_extra_carries_the_exact_sdk_pins",
    "the_sdist_withholds_them_too",
    "and_still_carries_every_shipped_package",
    # base-import purity against the installed artifact (story HB-X2-S2)
    "a_throwaway_virtualenv_was_created",
    "the_wheel_installs_with_no_index_and_no_dependency_resolution",
    "the_base_purity_probe_ran_to_completion",
    "no_banned_distribution_is_installed_beside_the_wheel",
    "importing_the_base_package_succeeds",
    "the_reference_flow_runs_from_the_installed_wheel",
    # the SDK seam, without the extra (story HB-X2-S2)
    "the_adapter_seam_probe_ran_to_completion",
    "importing_the_current_adapter_without_the_extra_succeeds",
    "the_sdk_seam_itself_imports_and_reports_the_sdk_absent",
    "require_sdk_raises_SdkUnavailable_naming_the_pins",
    "and_every_builder_behind_it_is_guarded_the_same_way",
    "the_legacy_adapter_runs_a_handshake_from_the_installed_wheel",
)


@pytest.fixture(scope="module")
def lane_report():
    """Run the whole build/install/probe lane once for this module."""
    # The one sanctioned skip: without a build backend there is no
    # artifact to assert against, and asserting against the source tree
    # instead would quietly turn this into the test it replaces.
    if importlib.util.find_spec("build") is None:  # pragma: no cover - env dependent
        pytest.skip("`build` is not installed; run tools/verify_wheel.py where it is")
    return load_lane().verify()


# ── the manifest ──────────────────────────────────────────────────────


def test_the_manifest_ships_all_three_packages() -> None:
    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for package in ("mcp_heartbeat", "mcp_heartbeat_current", "mcp_heartbeat_legacy"):
        assert f'"{package}"' in manifest and f'"{package}.*"' in manifest, (
            f"{package} is missing from [tool.setuptools.packages.find].include"
        )


def test_the_manifest_withholds_the_conformance_package_and_the_clean_room() -> None:
    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp_heartbeat_conformance"' in manifest.split("exclude = ")[-1], (
        "the conformance package must be excluded, not merely un-included"
    )
    # Mirrors the conformance matrix's own `cleanroom-not-shipped` case:
    # the independent implementation must not be nameable as a package.
    assert "hb_cleanroom" not in manifest


def test_the_manifest_declares_no_runtime_dependencies() -> None:
    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in manifest


def test_the_sdk_extra_stays_exactly_pinned() -> None:
    # `==` rather than a range, because these versions carry wire
    # constants: a patch release that moved an error code would change
    # the protocol the adapter claims to implement.
    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp==2.0.0"' in manifest
    assert '"mcp-types==2.0.0"' in manifest


def test_the_lane_exists_and_is_not_itself_shipped() -> None:
    assert LANE.is_file()
    assert LANE.parent.name == "tools"


# ── the artifacts ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name", REQUIRED_CHECKS)
def test_the_built_and_installed_artifact_holds_its_invariant(lane_report, name: str) -> None:
    matching = [entry for entry in lane_report.checks if entry["check"] == name]
    assert matching, (
        f"the lane never reported {name!r} — a probe stopped running. "
        f"Reported: {[entry['check'] for entry in lane_report.checks]}"
    )
    for entry in matching:
        assert entry["ok"], f"{name}: {entry['detail']}"


def test_no_check_in_the_lane_failed(lane_report) -> None:
    # The parametrised test above covers the named invariants; this one
    # catches anything the lane reports that this module has not been
    # taught about yet, so a new check cannot land silently unenforced.
    failed = [entry["check"] for entry in lane_report.checks if not entry["ok"]]
    assert failed == [], f"lane checks failed: {failed}"
