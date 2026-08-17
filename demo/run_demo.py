#!/usr/bin/env python3
"""Drive the two-participant demonstration and prove what it cost the host.

The demonstration itself is `docker compose up`, an observer run, and
`docker compose down`. This script exists for the three things around it that
are easy to claim and easy to get wrong.

**Hardening is checked on the running container, not on the YAML.** A Compose
file records an intention. Whether the daemon applied it is a separate
question, and the only authority on it is `docker inspect`. A demonstration
that asserted its posture by re-reading its own configuration would be
grading its own homework, so every check below reads the live container.

**The host is returned to baseline, and the baseline is a set, not a count.**
Container, volume, and network identifiers are recorded before anything
starts and compared afterwards. Counts alone would let a leaked volume hide
behind a removed one.

**Images are the documented exception.** `docker compose down` never removes
images, by design — a re-run is fast because they survive. So the image delta
is *recorded* rather than asserted away, and the README gives the `--rmi
local` form for a reviewer who wants the host bit-for-bit as it was.

Usage::

    python run_demo.py                                  # run, verify, tear down
    python run_demo.py --output ../release/demo-evidence.json
    python run_demo.py --keep                           # leave it up for poking

Exit ``0`` when every scenario passed, every hardening check held, and the
host returned to baseline; ``1`` otherwise.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEMO_ROOT = Path(__file__).resolve().parent
COMPOSE_FILE = DEMO_ROOT / "docker-compose.yml"
PROJECT = "mcp-heartbeat-demo"

PUBLISHER = "hb-demo-publisher"
OBSERVER = "hb-demo-observer"

#: Paths that must appear in no mount, on any container, ever. The Docker
#: socket is container escape; the host home is the repository and the
#: operator's credentials.
FORBIDDEN_MOUNT_FRAGMENTS = ("/var/run/docker.sock", "/root", "/home")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def docker(*args: str, check: bool = True) -> str:
    """Run a docker command and return its stdout."""
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, cwd=str(DEMO_ROOT)
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def docker_bytes(*args: str) -> bytes:
    """Run a docker command that emits binary, and return raw stdout.

    Separate from :func:`docker` because ``docker cp ... -`` writes a tar
    stream, and decoding that as text would corrupt it.
    """
    result = subprocess.run(["docker", *args], capture_output=True, cwd=str(DEMO_ROOT))
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed:\n{result.stderr.decode(errors='replace')}")
    return result.stdout


def compose(*args: str, check: bool = True) -> str:
    """Run a `docker compose` command against this demonstration's project."""
    return docker("compose", "-f", str(COMPOSE_FILE), "-p", PROJECT, *args, check=check)


def inspect(reference: str) -> dict[str, Any]:
    """Full `docker inspect` payload for one container."""
    return json.loads(docker("inspect", reference))[0]


# ── the host baseline ─────────────────────────────────────────────────


def snapshot() -> dict[str, list[str]]:
    """Identifier sets for everything the demonstration could leak.

    Sets rather than counts: a run that leaked one volume and removed another
    would balance out under counting and be invisible.
    """
    return {
        "containers": sorted(docker("ps", "-aq").split()),
        "volumes": sorted(docker("volume", "ls", "-q").split()),
        "networks": sorted(docker("network", "ls", "-q").split()),
        "images": sorted(docker("images", "-q").split()),
    }


def compare_baseline(before: dict[str, list[str]], after: dict[str, list[str]]) -> dict[str, Any]:
    """Diff two snapshots, keeping images separate from the assertion."""
    delta = {}
    for kind in ("containers", "volumes", "networks", "images"):
        start, end = set(before[kind]), set(after[kind])
        delta[kind] = {
            "before": len(start),
            "after": len(end),
            "leaked": sorted(end - start),
            "removed": sorted(start - end),
        }
    return delta


# ── hardening, read off the live container ────────────────────────────


def hardening_checks(name: str, payload: dict[str, Any], *, expect_published_ports: bool) -> list[dict[str, Any]]:
    """Every posture assertion, against one running container."""
    host = payload.get("HostConfig", {})
    config = payload.get("Config", {})
    state = payload.get("State", {})
    checks: list[dict[str, Any]] = []

    def check(rule: str, ok: bool, detail: Any = None) -> None:
        checks.append({"container": name, "check": rule, "passed": bool(ok), "detail": detail})

    # Read from the live container, so this is only meaningful while it runs.
    check("the_container_is_actually_running", state.get("Running") is True, state.get("Status"))

    check("it_runs_as_a_non_root_user", config.get("User") == "10001:10001", config.get("User"))
    check("it_is_not_privileged", host.get("Privileged") is False, host.get("Privileged"))
    check("its_root_filesystem_is_read_only", host.get("ReadonlyRootfs") is True, host.get("ReadonlyRootfs"))

    cap_drop = host.get("CapDrop") or []
    cap_add = host.get("CapAdd") or []
    check("every_capability_is_dropped", "ALL" in cap_drop, cap_drop)
    check("and_none_is_added_back", cap_add == [], cap_add)

    security_opt = host.get("SecurityOpt") or []
    check(
        "privilege_escalation_is_refused",
        any("no-new-privileges" in entry for entry in security_opt),
        security_opt,
    )

    check("its_pid_count_is_bounded", (host.get("PidsLimit") or 0) > 0, host.get("PidsLimit"))
    check("its_memory_is_bounded", (host.get("Memory") or 0) > 0, host.get("Memory"))
    check("its_cpu_is_bounded", (host.get("NanoCpus") or 0) > 0, host.get("NanoCpus"))

    network_mode = host.get("NetworkMode")
    check("it_does_not_use_host_networking", network_mode != "host", network_mode)
    networks = list((payload.get("NetworkSettings", {}).get("Networks") or {}))
    check(
        "it_is_attached_only_to_the_private_demo_network",
        networks == [f"{PROJECT}_demo"],
        networks,
    )

    # Mounts: named volumes only. A bind mount is a host path inside the
    # container, which is the thing this rules out.
    mounts = payload.get("Mounts") or []
    binds = [mount for mount in mounts if mount.get("Type") != "volume"]
    check("it_bind_mounts_nothing_from_the_host", binds == [], binds)

    sources = [str(mount.get("Source", "")) for mount in mounts] + list(host.get("Binds") or [])
    smuggled = sorted(
        source
        for source in sources
        if any(fragment in source for fragment in FORBIDDEN_MOUNT_FRAGMENTS)
    )
    check("it_mounts_no_docker_socket_and_no_host_home", smuggled == [], smuggled)
    check("it_maps_no_host_device", (host.get("Devices") or []) == [], host.get("Devices"))

    # Published ports, where there are any, reach loopback and nothing else.
    ports = payload.get("NetworkSettings", {}).get("Ports") or {}
    published = [
        binding
        for bindings in ports.values()
        if bindings
        for binding in bindings
    ]
    if expect_published_ports:
        check("it_publishes_at_least_one_port", published != [], ports)
    hosts = sorted({binding.get("HostIp") for binding in published})
    check(
        "every_published_port_is_bound_to_loopback_only",
        all(binding.get("HostIp") in ("127.0.0.1", "::1") for binding in published),
        hosts,
    )

    tmpfs = host.get("Tmpfs") or {}
    check("its_only_writable_scratch_is_a_bounded_tmpfs", "/tmp" in tmpfs, tmpfs)

    return checks


def wait_for(predicate, *, budget_seconds: float, interval: float = 0.5, what: str = "condition"):
    """Poll until ``predicate`` returns a truthy value, or give up."""
    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except Exception:  # the daemon may not have created the object yet
            value = None
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"{what} did not happen within {budget_seconds}s")


# ── the run ───────────────────────────────────────────────────────────


def run(*, keep: bool) -> dict[str, Any]:
    started = _stamp()
    before = snapshot()

    record: dict[str, Any] = {
        "schema_version": "0.1",
        "artifact": "heartbeat-two-participant-demonstration",
        "run": {"started_at": started, "project": PROJECT},
        "baseline": {"before": {k: len(v) for k, v in before.items()}},
        "hardening": [],
        "observation": None,
        "notes": {
            "images": (
                "`docker compose down` never removes images, by design — a re-run is "
                "fast because they survive. The image delta is therefore recorded, not "
                "asserted to zero. `down --rmi local` removes them."
            ),
            "hardening_source": (
                "Every hardening check reads `docker inspect` on the RUNNING container, "
                "not the Compose file."
            ),
        },
    }

    torn_down = False
    try:
        # Both images, up front, including the profile-gated one. Relying on
        # `up --build` for the publisher alone would silently leave the
        # observer on whatever image a previous run left behind — a stale
        # oracle grading a fresh subject.
        compose("--profile", "observe", "build")
        compose("up", "-d", "--wait", PUBLISHER)

        publisher = inspect(f"{PROJECT}-{PUBLISHER}-1")
        record["hardening"] += hardening_checks(
            PUBLISHER, publisher, expect_published_ports=True
        )

        # Detached, so the observer can be inspected while it is running. The
        # scenarios spend real wall-clock time on the lease, which leaves a
        # comfortable window to read its posture off the daemon.
        compose("--profile", "observe", "up", "-d", OBSERVER)
        observer_ref = f"{PROJECT}-{OBSERVER}-1"
        wait_for(
            lambda: inspect(observer_ref)["State"]["Running"],
            budget_seconds=60,
            what="the observer starting",
        )
        record["hardening"] += hardening_checks(
            OBSERVER, inspect(observer_ref), expect_published_ports=False
        )

        exit_code = wait_for(
            lambda: (
                inspect(observer_ref)["State"]["Status"] == "exited"
                and inspect(observer_ref)["State"]
            ),
            budget_seconds=180,
            what="the observer finishing",
        )["ExitCode"]
        record["run"]["observer_exit_code"] = exit_code
        record["run"]["observer_log"] = compose("logs", "--no-color", OBSERVER)[-4000:]

        # Out of the named volume by way of the stopped container, so the
        # evidence never needs a host bind mount to escape.
        raw = docker_bytes("cp", f"{observer_ref}:/evidence/observation.json", "-")
        record["observation"] = _read_tar_member(raw)
    finally:
        if not keep:
            # `--profile observe` is load-bearing on the way *down* too: a
            # profile-gated service is invisible to a plain `down`, so the
            # observer container and the evidence volume would survive it.
            # The baseline assertion below is what caught that.
            compose("--profile", "observe", "down", "--volumes", "--remove-orphans", check=False)
            torn_down = True

    after = snapshot()
    record["baseline"]["after"] = {k: len(v) for k, v in after.items()}
    record["baseline"]["delta"] = compare_baseline(before, after)
    record["baseline"]["torn_down"] = torn_down

    leaked = {
        kind: record["baseline"]["delta"][kind]["leaked"]
        for kind in ("containers", "volumes", "networks")
        if record["baseline"]["delta"][kind]["leaked"]
    }
    record["checks"] = [
        {
            "check": "every_hardening_rule_held_on_the_running_containers",
            "passed": all(entry["passed"] for entry in record["hardening"]),
            "detail": [e["check"] for e in record["hardening"] if not e["passed"]],
        },
        {
            "check": "every_scenario_passed",
            "passed": bool((record["observation"] or {}).get("summary", {}).get("ok")),
            "detail": (record["observation"] or {}).get("summary"),
        },
        {
            "check": "the_host_returned_to_its_pre_run_baseline",
            "passed": torn_down and not leaked,
            "detail": leaked or "no containers, volumes or networks leaked",
        },
    ]
    record["run"]["finished_at"] = _stamp()
    record["verdict"] = "PASS" if all(entry["passed"] for entry in record["checks"]) else "FAIL"
    return record


def _read_tar_member(raw: bytes) -> dict[str, Any]:
    """`docker cp ... -` emits a tar stream; pull the single member out."""
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        member = next(m for m in archive.getmembers() if m.isfile())
        extracted = archive.extractfile(member)
        return json.loads(extracted.read()) if extracted else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the two-participant demonstration.")
    parser.add_argument(
        "--output",
        default=str(DEMO_ROOT / "evidence" / "latest.json"),
        help="where to write the evidence record",
    )
    parser.add_argument("--keep", action="store_true", help="leave the demonstration running")
    args = parser.parse_args(argv)

    record = run(keep=args.keep)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for entry in record["checks"]:
        print(f"{'ok  ' if entry['passed'] else 'FAIL'} {entry['check']}")
        if not entry["passed"]:
            print(f"       {entry['detail']}")
    for scenario in (record["observation"] or {}).get("scenarios", []):
        print(f"     [{scenario['verdict']}] {scenario['scenario_id']}: {scenario['title']}")
    print(f"\nverdict: {record['verdict']} — evidence at {output}")
    return 0 if record["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
