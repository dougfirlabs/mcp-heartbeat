"""The release artifacts: reproducible, correctly described, honestly claimed.

Three things are under test here, and they fail in different ways.

* **Reproducibility** is a gate. `tools/build_release.py` builds the wheel and
  the sdist twice from deliberately unalike inputs and the digests have to
  agree — with each other, and with the committed `release/SHA256SUMS`. That
  lane is slow, so it runs once per module and every assertion below reads its
  report.

* **The SBOM** is checked for what it must *not* say as much as for what it
  must. Every component has to be optional and has to name the extra that
  pulls it in, because the base install pulls in nothing; and nothing may be
  listed that the built wheel does not declare.

* **The release notes** are checked for the three caveats that are the whole
  reason a 0.1 needs notes: the identifier is experimental, the wire naming is
  provisional, and nothing has been published. Those are easy to soften by
  accident in an edit, and softening them is the failure that matters.

The reproducibility lane is deliberately the same code path an operator runs
by hand. A test that reimplemented the build would be testing itself.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = PACKAGE_ROOT / "release"
LANE = PACKAGE_ROOT / "tools" / "build_release.py"

#: Two full builds plus two virtualenv-free archive rewrites. Slower than the
#: rest of this suite by well over an order of magnitude.
pytestmark = pytest.mark.timeout(600)


def load_lane():
    """Import ``tools/build_release.py`` by path.

    By path for the same reason ``test_packaging.py`` does it: ``tools/`` is
    not a package and deliberately does not ship, so importing it normally
    would mean putting the tooling somewhere the wheel could reach.
    """
    spec = importlib.util.spec_from_file_location("hb_build_release", LANE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lane():
    return load_lane()


@pytest.fixture(scope="module")
def lane_report(lane):
    """Run the whole double-build lane once for this module."""
    if importlib.util.find_spec("build") is None:  # pragma: no cover - env dependent
        pytest.skip("`build` is not installed; run tools/build_release.py where it is")
    # `check_only` so the suite never rewrites the committed release files.
    # A test that regenerated its own expectation could not fail.
    return lane.run(check_only=True)


@pytest.fixture(scope="module")
def sbom():
    return json.loads((RELEASE_DIR / "sbom.json").read_text(encoding="utf-8"))


# ── reproducibility ───────────────────────────────────────────────────


REQUIRED_CHECKS = (
    "the_two_independent_builds_agree_byte_for_byte",
    "the_rebuild_matches_the_committed_checksums",
    "the_wheel_declares_no_unconditional_dependency",
)


@pytest.mark.parametrize("name", REQUIRED_CHECKS)
def test_the_release_lane_holds_its_invariant(lane_report, name: str) -> None:
    matching = [entry for entry in lane_report["checks"] if entry["check"] == name]
    assert matching, (
        f"the lane never reported {name!r}. "
        f"Reported: {[entry['check'] for entry in lane_report['checks']]}"
    )
    for entry in matching:
        assert entry["ok"], f"{name}: {entry['detail']}"


def test_both_artifacts_were_built_twice(lane_report) -> None:
    # Two passes, and each pass produced both artifacts. A lane that quietly
    # built once would still satisfy every digest comparison above, because
    # a value always equals itself.
    assert len(lane_report["builds"]) == 2
    for build in lane_report["builds"]:
        assert len(build["artifacts"]) == 2, build

    first, second = lane_report["builds"]
    assert first["source_dir"] != second["source_dir"], (
        "both passes built from the same directory, so an embedded build path "
        "could not have been detected"
    )


def test_the_committed_checksums_cover_exactly_the_shipped_artifacts(lane) -> None:
    committed = lane.parse_sha256sums((RELEASE_DIR / "SHA256SUMS").read_text(encoding="utf-8"))
    assert sorted(committed) == [
        "mcp_heartbeat-0.1.0-py3-none-any.whl",
        "mcp_heartbeat-0.1.0.tar.gz",
    ]
    for name, digest in committed.items():
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef"), (name, digest)


def test_the_reproducibility_record_scopes_its_claim_to_a_toolchain() -> None:
    # The honest caveat is load-bearing. A different Python or setuptools may
    # emit different bytes and still be correct, so the record has to say
    # which one the digests belong to.
    record = json.loads((RELEASE_DIR / "reproducibility.json").read_text(encoding="utf-8"))
    assert record["verdict"] == "PASS"
    for key in ("python", "setuptools", "build"):
        assert record["toolchain"][key]
    assert record["source_date_epoch"] == 1786838400


def test_the_source_date_epoch_is_a_constant_not_a_computed_value() -> None:
    # A timestamp derived from the clock, the git log, or the file system is
    # a different number on every host, which is the whole failure the
    # constant exists to remove.
    source = LANE.read_text(encoding="utf-8")
    assert "SOURCE_DATE_EPOCH = 1786838400" in source


# ── the SBOM ──────────────────────────────────────────────────────────


def test_the_sbom_is_a_well_formed_cyclonedx_document(sbom) -> None:
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["serialNumber"].startswith("urn:uuid:")
    assert sbom["metadata"]["component"]["name"] == "mcp-heartbeat"
    assert sbom["metadata"]["component"]["version"] == "0.1.0"


def test_the_sbom_lists_no_required_dependency(sbom) -> None:
    # The package's central claim, restated where an SBOM consumer will look
    # for it. `dependencies = []` in the manifest is the intent;
    # `scope: optional` on every component here is what a consumer reads.
    required = [c for c in sbom["components"] if c.get("scope") != "optional"]
    assert required == [], f"these would install unconditionally: {required}"


def test_every_sbom_component_names_the_extra_that_pulls_it_in(sbom) -> None:
    for component in sbom["components"]:
        properties = {p["name"]: p["value"] for p in component["properties"]}
        assert properties.get("python:extra"), component["name"]
        assert properties.get("python:requires-dist"), component["name"]


def test_the_sbom_records_the_sdk_pins_exactly(sbom) -> None:
    current = {
        component["name"]: component.get("version")
        for component in sbom["components"]
        if {p["name"]: p["value"] for p in component["properties"]}["python:extra"] == "current"
    }
    assert current == {"mcp": "2.0.0", "mcp-types": "2.0.0"}


def test_the_sbom_does_not_resolve_a_range_into_a_version(sbom) -> None:
    # A range says which versions are *acceptable*, not which one ships.
    # Printing its lower bound as `version` would assert a fact nobody
    # established.
    for component in sbom["components"]:
        properties = {p["name"]: p["value"] for p in component["properties"]}
        if properties["python:pinned"] == "false":
            assert "version" not in component, component["name"]
            assert "@" not in component["purl"], component["purl"]
        else:
            assert properties["python:version-specifier"].startswith("==")


def test_the_sbom_describes_the_artifact_that_is_actually_committed(sbom, lane) -> None:
    committed = lane.parse_sha256sums((RELEASE_DIR / "SHA256SUMS").read_text(encoding="utf-8"))
    hashes = {entry["alg"]: entry["content"] for entry in sbom["metadata"]["component"]["hashes"]}
    assert hashes["SHA-256"] == committed["mcp_heartbeat-0.1.0-py3-none-any.whl"]

    properties = {p["name"]: p["value"] for p in sbom["metadata"]["component"]["properties"]}
    assert properties["release:sdist-sha256"] == committed["mcp_heartbeat-0.1.0.tar.gz"]
    assert properties["python:shipped-packages"] == (
        "mcp_heartbeat, mcp_heartbeat_current, mcp_heartbeat_legacy"
    )


def test_the_sbom_is_deterministic(sbom, lane_report) -> None:
    # It carries no clock and no random serial, so a rebuild rebuilds it
    # byte-for-byte. An SBOM that changed on every build could not be
    # checked into the release it describes.
    assert sbom == lane_report["sbom"]


# ── the drafted release notes ─────────────────────────────────────────


@pytest.fixture(scope="module")
def notes() -> str:
    return (RELEASE_DIR / "RELEASE-NOTES-0.1.0.md").read_text(encoding="utf-8")


def test_the_release_notes_are_marked_a_draft(notes: str) -> None:
    assert "DRAFT" in notes.split("\n")[0] or "**DRAFT**" in notes[:400]


def test_the_release_notes_disclaim_every_form_of_publication(notes: str) -> None:
    # Each of these is a separate operator-gated decision, and the notes are
    # the document most likely to be read as though one had been taken.
    lowered = notes.lower()
    for claim in ("no git tag", "pypi", "registry", "visibility"):
        assert claim in lowered, f"the notes no longer disclaim {claim!r}"


def test_the_release_notes_carry_the_experimental_identifier_caveat(notes: str) -> None:
    assert "com.dougfirlabs/heartbeat" in notes
    assert "experimental" in notes.lower()
    assert "not** registered" in notes or "not registered" in notes


def test_the_release_notes_keep_the_provisional_naming_status(notes: str) -> None:
    # `node_id`/`boot_id` keep their wire spelling at 0.1 and a rename is
    # deferred to 1.0. A reader planning against this needs that.
    assert "provisional" in notes.lower()
    assert "node_id" in notes and "boot_id" in notes
    assert "1.0" in notes


def test_the_release_notes_state_the_supported_mcp_eras(notes: str) -> None:
    for era in ("2026-07-28", "2025-06-18", "2025-03-26"):
        assert era in notes, f"the era table no longer mentions {era}"


def test_the_release_notes_do_not_claim_a_permission_the_package_refuses(notes: str) -> None:
    # `freshness is not permission` is the one sentence the whole package
    # exists to defend, and the absence of `can_dispatch` is how it is kept.
    # The notes may only mention that name to say there isn't one — so every
    # occurrence has to sit next to a denial rather than a description.
    assert "freshness is not permission" in notes.lower()
    # Whitespace-collapsed, because the denial and the name land on different
    # lines once the prose is wrapped.
    flowed = " ".join(notes.split())
    assert flowed.count("can_dispatch") == flowed.count("no `can_dispatch` API"), (
        "the notes mention can_dispatch somewhere other than to deny it exists"
    )
