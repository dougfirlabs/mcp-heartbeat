#!/usr/bin/env python3
"""Prove the distribution's invariants against the *built* artifacts.

One wheel ships the portable core and both era adapters. That is a wider
distribution than the core alone, and it is safe only while three things
stay true:

* ``import mcp_heartbeat`` needs nothing but the standard library;
* ``import mcp_heartbeat_current`` needs nothing but the standard library
  too — the official SDK is an optional extra, and its absence must fail
  at the ``sdk`` seam with an actionable message, never at import;
* the conformance tooling and the clean-room implementation — the things
  that *check* the package rather than things the package *is* — do not
  ship.

Every one of those is asserted here against a wheel that was built and
installed, not against the source tree and not against ``pyproject.toml``.
Reading the manifest would only prove the intent was written down; the
question is what the artifact does. So this script builds the wheel and
the sdist, installs the wheel into a throwaway virtualenv, and runs the
probes inside that interpreter.

Why a throwaway venv rather than the caller's: for the same reason
``verify_sdk.sh`` uses one. An environment that already has this package
installed cannot prove an import succeeded *without* it, and installing
the ``current`` extra into the shared venv would resolve ``pydantic-core``
off the version its ``pydantic`` pins.

Usage::

    tools/verify_wheel.py            # human report
    tools/verify_wheel.py --json -   # evidence to stdout
    tools/verify_wheel.py --keep     # leave the build dir

Exit ``0`` when every check passes, ``1`` otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

#: The three packages the single wheel is supposed to ship.
SHIPPED_PACKAGES = ("mcp_heartbeat", "mcp_heartbeat_current", "mcp_heartbeat_legacy")

#: Path fragments that must appear in neither artifact. The conformance
#: package and the tooling verify the distribution; the clean room is an
#: independent second implementation used to check the first. A verifier
#: shipped inside the artifact it verifies is checking itself.
WITHHELD_FRAGMENTS = ("mcp_heartbeat_conformance", "hb_cleanroom", "tools/")

#: Roots banned inside the probe interpreter. ``mcp`` and ``pydantic`` are the
#: PRD's list; ``mcp_types`` rides along because it is the other half of
#: the SDK pin and letting it through would leave the guard half-open.
BANNED_ROOTS = (
    "mcp",
    "mcp_types",
    "pydantic",
)

#: The exact pins the ``current`` extra must carry. They are ``==`` on
#: purpose: these versions carry wire constants, so a range would let a
#: patch release move an error code out from under the adapter.
EXPECTED_EXTRA_PINS = ("mcp==2.0.0", "mcp-types==2.0.0")


# ── the probes, run inside the throwaway venv ─────────────────────────
#
# Each probe prints one JSON object per check to stdout, so a failure
# names the check that failed rather than dumping a traceback the caller
# has to interpret. The guard is prepended to both: it refuses every
# banned root at the meta-path, ahead of the real finders, which means a
# probe cannot pass by accident just because nothing happened to be
# installed. It has to pass because nothing was *reachable*.

_GUARD = '''
import json
import sys

BANNED = {banned!r}


class _PurityGuard:
    """Refuse every banned root before any real finder sees it."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BANNED:
            raise ImportError("refused by the purity guard: " + fullname)
        return None


sys.meta_path.insert(0, _PurityGuard())


def report(check, ok, detail=None):
    print(json.dumps({{"check": check, "ok": bool(ok), "detail": detail}}))


def leaked():
    return sorted(set(BANNED) & {{name.split(".")[0] for name in sys.modules}})
'''

BASE_PROBE = _GUARD + '''
import importlib.metadata as md

installed = sorted(dist.metadata["Name"] or "" for dist in md.distributions())
normalised = {{name.lower().replace("-", "_") for name in installed}}
report(
    "no_banned_distribution_is_installed_beside_the_wheel",
    not (normalised & set(BANNED)),
    installed,
)

import mcp_heartbeat as hb

report("importing_the_base_package_succeeds", True, hb.__file__)
report("and_it_pulled_in_no_banned_module", not leaked(), leaked())

# An import that does nothing proves less than a run that does something.
# This is the README's reference flow, executed from the installed wheel.
clock = hb.FakeClock()
issuer = hb.HeartbeatIssuer(participant_id="wheel/probe-1", epoch_id="e1", clock=clock)
state = hb.LineageState(participant_id="wheel/probe-1")

outcome = hb.admit(state, issuer.issue().to_dict(), clock.now())
ok = outcome.accepted
clock.advance(1)
outcome = hb.admit(outcome.state, issuer.issue().to_dict(), clock.now())
report(
    "the_reference_flow_runs_from_the_installed_wheel",
    ok and outcome.accepted and outcome.state.held.sequence == 1,
    getattr(outcome, "reason", None),
)
report("and_the_flow_pulled_in_no_banned_module", not leaked(), leaked())
'''

ADAPTER_PROBE = _GUARD + '''
import mcp_heartbeat_current as current

report("importing_the_current_adapter_without_the_extra_succeeds", True, current.__version__)
report("and_it_pulled_in_no_banned_module", not leaked(), leaked())

from mcp_heartbeat_current import sdk

report("the_sdk_seam_itself_imports_and_reports_the_sdk_absent", sdk.SDK_AVAILABLE is False)

# The seam must fail *here*, and it must say what to install. An
# obscure ModuleNotFoundError three frames deep is the failure mode
# this whole arrangement exists to avoid.
try:
    sdk.require_sdk()
except sdk.SdkUnavailable as exc:
    message = str(exc)
else:
    message = None

report(
    "require_sdk_raises_SdkUnavailable_naming_the_pins",
    message is not None and all(pin in message for pin in {pins!r}),
    message,
)

try:
    sdk.build_heartbeat_extension()
except sdk.SdkUnavailable:
    builder_guarded = True
except Exception as exc:  # pragma: no cover - the failure we are ruling out
    builder_guarded = "unexpected: " + type(exc).__name__ + ": " + str(exc)
else:
    builder_guarded = False

report("and_every_builder_behind_it_is_guarded_the_same_way", builder_guarded is True, builder_guarded)

# The legacy adapter has no optional dependency at all, so it must not
# merely import — it must work, straight out of the wheel.
from mcp_heartbeat_legacy import LegacyClientSession, LegacyServerSession

server = LegacyServerSession(server_name="wheel-probe", implemented={{"resources/read"}})
client = LegacyClientSession(client_name="probe")
handshake = client.consume_initialize_result(server.handle(*client.initialize_request()))
server.handle(*client.initialized_notification())

report(
    "the_legacy_adapter_runs_a_handshake_from_the_installed_wheel",
    handshake.mcp_protocol_era == "2025-06-18" and handshake.extension_version == "0.1",
    {{"era": handshake.mcp_protocol_era, "extension_version": handshake.extension_version}},
)
report("and_neither_adapter_pulled_in_a_banned_module", not leaked(), leaked())
'''


# ── the report ────────────────────────────────────────────────────────


@dataclass
class Report:
    """Ordered checks plus the artifact facts they were derived from."""

    checks: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, ok: bool, detail: Any = None) -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    @property
    def passed(self) -> bool:
        return all(entry["ok"] for entry in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": "wheel-distribution-conformance",
            "verdict": "PASS" if self.passed else "FAIL",
            "shipped_packages": list(SHIPPED_PACKAGES),
            "withheld_fragments": list(WITHHELD_FRAGMENTS),
            "banned_roots": list(BANNED_ROOTS),
            "artifacts": self.artifacts,
            "checks": self.checks,
        }


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with ``PYTHONPATH`` cleared.

    The ambient value on a dev box points at a parent repository's ``src``, and a
    purity proof that could import it would not be proving what it
    claims to.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(command, capture_output=True, text=True, env=env, **kwargs)


def build_artifacts(outdir: Path, report: Report) -> tuple[Path | None, Path | None]:
    """Build the wheel and the sdist into ``outdir``.

    ``--no-isolation`` on purpose: the build backend is already present in
    the calling interpreter, and an isolated build would reach for the
    network — which would make this lane fail for a reason that has
    nothing to do with the package.
    """
    result = _run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(outdir), str(PACKAGE_ROOT)],
        cwd=str(PACKAGE_ROOT),
    )
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    report.check(
        "the_package_builds_one_wheel_and_one_sdist",
        result.returncode == 0 and len(wheels) == 1 and len(sdists) == 1,
        result.stderr[-2000:] if result.returncode != 0 else [p.name for p in wheels + sdists],
    )
    return (wheels[0] if wheels else None), (sdists[0] if sdists else None)


def inspect_wheel(wheel: Path, report: Report) -> None:
    """Assert what the wheel ships, what it withholds, and what it requires."""
    with zipfile.ZipFile(wheel) as archive:
        names = sorted(archive.namelist())
        metadata_name = next(
            (n for n in names if n.endswith(".dist-info/METADATA")),
            None,
        )
        metadata = archive.read(metadata_name).decode("utf-8") if metadata_name else ""

    report.artifacts["wheel"] = {"name": wheel.name, "entries": names}

    top_level = sorted(
        {
            root
            for root in (name.split("/")[0] for name in names)
            if not root.endswith(".dist-info")
        }
    )
    shipped = [pkg for pkg in SHIPPED_PACKAGES if f"{pkg}/__init__.py" in names]
    report.check("the_wheel_ships_all_three_packages", shipped == list(SHIPPED_PACKAGES), top_level)

    smuggled = sorted(
        name for name in names if any(fragment in name for fragment in WITHHELD_FRAGMENTS)
    )
    report.check("and_withholds_the_conformance_tooling_and_clean_room", smuggled == [], smuggled)

    requires = [
        line.partition(":")[2].strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]
    unconditional = [line for line in requires if "extra ==" not in line]
    report.check(
        "no_dependency_is_unconditional_in_the_built_metadata",
        unconditional == [],
        unconditional,
    )

    current_extra = [line for line in requires if 'extra == "current"' in line]
    pinned = sorted(line.split(";")[0].strip() for line in current_extra)
    report.check(
        "the_current_extra_carries_the_exact_sdk_pins",
        pinned == sorted(EXPECTED_EXTRA_PINS),
        pinned,
    )
    report.artifacts["wheel"]["requires_dist"] = requires


def inspect_sdist(sdist: Path, report: Report) -> None:
    """The sdist withholds the same things the wheel does."""
    with tarfile.open(sdist) as archive:
        names = sorted(archive.getnames())

    report.artifacts["sdist"] = {"name": sdist.name, "entries": names}

    # Compared on the path *inside* the distribution root, so the
    # ``mcp_heartbeat-0.1.0/`` prefix cannot mask a match.
    stripped = [name.partition("/")[2] for name in names]
    smuggled = sorted(
        name
        for name in stripped
        if name and any(fragment in name for fragment in WITHHELD_FRAGMENTS)
    )
    report.check("the_sdist_withholds_them_too", smuggled == [], smuggled)

    report.check(
        "and_still_carries_every_shipped_package",
        all(f"src/{pkg}/__init__.py" in stripped for pkg in SHIPPED_PACKAGES),
        [name for name in stripped if name.startswith("src/")][:5],
    )


def install_into_throwaway_venv(wheel: Path, root: Path, report: Report) -> Path | None:
    """Create an empty venv and install nothing but the wheel.

    ``--no-index`` is the load-bearing flag: it makes a network fetch
    impossible, so anything importable afterwards came out of the wheel.
    """
    venv = root / "venv"
    created = _run([sys.executable, "-m", "venv", str(venv)])
    python = venv / "bin" / "python"
    if created.returncode != 0 or not python.exists():
        report.check("a_throwaway_virtualenv_was_created", False, created.stderr[-2000:])
        return None
    report.check("a_throwaway_virtualenv_was_created", True, str(venv))

    installed = _run([str(python), "-m", "pip", "install", "--quiet", "--no-index", str(wheel)])
    report.check(
        "the_wheel_installs_with_no_index_and_no_dependency_resolution",
        installed.returncode == 0,
        installed.stderr[-2000:] if installed.returncode else wheel.name,
    )
    return python if installed.returncode == 0 else None


def run_probe(python: Path, source: str, root: Path, name: str, report: Report) -> None:
    """Execute one probe in the throwaway interpreter and fold in its checks."""
    script = root / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    # cwd is the temp root, not the package: running from the package
    # would put its directory on ``sys.path`` and blur which copy of the
    # code the probe actually imported.
    result = _run([str(python), str(script)], cwd=str(root))

    if result.returncode != 0:
        report.check(f"the_{name}_probe_ran_to_completion", False, result.stderr[-2000:])
        return
    report.check(f"the_{name}_probe_ran_to_completion", True)

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:  # pragma: no cover - a probe printing prose
            report.check(f"the_{name}_probe_emitted_parsable_checks", False, line)
            continue
        report.checks.append(entry)


def verify(keep: bool = False) -> Report:
    """Build, install, probe, and inspect. Returns the filled report."""
    report = Report()
    root = Path(tempfile.mkdtemp(prefix="mcp-heartbeat-wheel-"))
    try:
        dist = root / "dist"
        dist.mkdir()
        wheel, sdist = build_artifacts(dist, report)
        if wheel is None or sdist is None:
            return report

        inspect_wheel(wheel, report)
        inspect_sdist(sdist, report)

        python = install_into_throwaway_venv(wheel, root, report)
        if python is None:
            return report

        run_probe(python, BASE_PROBE.format(banned=BANNED_ROOTS), root, "base_purity", report)
        run_probe(
            python,
            ADAPTER_PROBE.format(banned=BANNED_ROOTS, pins=EXPECTED_EXTRA_PINS),
            root,
            "adapter_seam",
            report,
        )
        return report
    finally:
        if keep:
            print(f"kept: {root}", file=sys.stderr)
        else:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove the distribution's invariants against the built artifacts."
    )
    parser.add_argument("--json", metavar="PATH", help="write the evidence record ('-' for stdout)")
    parser.add_argument("--keep", action="store_true", help="leave the build directory in place")
    args = parser.parse_args(argv)

    report = verify(keep=args.keep)

    if args.json == "-":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        for entry in report.checks:
            mark = "ok  " if entry["ok"] else "FAIL"
            print(f"{mark} {entry['check']}")
            if not entry["ok"]:
                print(f"       {entry['detail']}")
        print(f"\nverdict: {'PASS' if report.passed else 'FAIL'} ({len(report.checks)} checks)")
        if args.json:
            Path(args.json).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
            print(f"evidence: {args.json}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
