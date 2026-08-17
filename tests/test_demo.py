"""The two-participant demonstration, checked against its recorded run.

The demonstration needs Docker, spends real wall-clock time on a lease, and
is therefore not something a unit suite should start. So this module asserts
against the **committed evidence record** of a run that happened —
``release/demo-evidence.json`` — the same way ``test_packaging.py`` asserts
against a built artifact rather than against ``pyproject.toml``.

That choice has a sharp edge, and it is worth naming: a committed record can
go stale. So the checks below are written to fail when the record stops
describing *this* demonstration — wrong scenarios, missing hardening rules,
a container that was not actually running when it was inspected. A stale
record cannot quietly keep passing.

The compose file gets a cheap static canary too. It is not the real proof —
``run_demo.py`` re-reads every rule off the running container with
``docker inspect``, and the recorded result of that is what the hardening
tests below read. The canary exists to fail with a one-line diff when someone
edits the YAML, which is a friendlier first failure than a container that
comes up wrong.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = PACKAGE_ROOT / "demo"
EVIDENCE = PACKAGE_ROOT / "release" / "demo-evidence.json"
COMPOSE = DEMO_ROOT / "docker-compose.yml"

#: The scenarios the demonstration must run, in order. Listed explicitly so a
#: scenario that silently stopped running fails here rather than passing by
#: producing nothing.
REQUIRED_SCENARIOS = (
    "handshake",
    "renewal",
    "status-transition",
    "expiry",
    "recovery",
)

#: Every hardening rule that must appear for *each* container. These are the
#: PRD's posture requirements, restated as the names `run_demo.py` reports.
REQUIRED_HARDENING = (
    "the_container_is_actually_running",
    "it_runs_as_a_non_root_user",
    "it_is_not_privileged",
    "its_root_filesystem_is_read_only",
    "every_capability_is_dropped",
    "and_none_is_added_back",
    "privilege_escalation_is_refused",
    "its_pid_count_is_bounded",
    "its_memory_is_bounded",
    "its_cpu_is_bounded",
    "it_does_not_use_host_networking",
    "it_is_attached_only_to_the_private_demo_network",
    "it_bind_mounts_nothing_from_the_host",
    "it_mounts_no_docker_socket_and_no_host_home",
    "it_maps_no_host_device",
    "every_published_port_is_bound_to_loopback_only",
    "its_only_writable_scratch_is_a_bounded_tmpfs",
)


@pytest.fixture(scope="module")
def evidence() -> dict:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


# ── the recorded run ──────────────────────────────────────────────────


def test_the_demonstration_passed(evidence) -> None:
    assert evidence["verdict"] == "PASS"
    assert evidence["artifact"] == "heartbeat-two-participant-demonstration"
    failed = [entry["check"] for entry in evidence["checks"] if not entry["passed"]]
    assert failed == [], failed


def test_the_observer_ran_every_scenario_and_passed_it(evidence) -> None:
    observed = tuple(entry["scenario_id"] for entry in evidence["observation"]["scenarios"])
    assert observed == REQUIRED_SCENARIOS, observed
    for scenario in evidence["observation"]["scenarios"]:
        failed = [c["name"] for c in scenario["checks"] if not c["passed"]]
        assert failed == [], f"{scenario['scenario_id']}: {failed}"
        assert scenario["checks"], f"{scenario['scenario_id']} asserted nothing"
    assert evidence["observation"]["summary"]["ok"] is True
    assert evidence["run"]["observer_exit_code"] == 0


def test_the_two_participants_are_genuinely_two(evidence) -> None:
    # A "two-participant" demonstration that ran one container would satisfy
    # every check above.
    containers = {entry["container"] for entry in evidence["hardening"]}
    assert containers == {"hb-demo-publisher", "hb-demo-observer"}, containers


@pytest.mark.parametrize("container", ["hb-demo-publisher", "hb-demo-observer"])
@pytest.mark.parametrize("rule", REQUIRED_HARDENING)
def test_every_hardening_rule_was_checked_on_the_running_container(
    evidence, container: str, rule: str
) -> None:
    matching = [
        entry
        for entry in evidence["hardening"]
        if entry["container"] == container and entry["check"] == rule
    ]
    assert matching, f"{container} never reported {rule!r}"
    for entry in matching:
        assert entry["passed"], f"{container}/{rule}: {entry.get('detail')}"


def test_the_posture_was_read_off_a_live_container_not_the_compose_file(evidence) -> None:
    # The whole argument for `docker inspect` is that a Compose file records
    # an intention. If the container was not running when it was inspected,
    # every other hardening check is describing a corpse.
    running = [
        entry for entry in evidence["hardening"] if entry["check"] == "the_container_is_actually_running"
    ]
    assert len(running) == 2
    assert all(entry["passed"] for entry in running)
    assert all(entry["detail"] == "running" for entry in running), running


def test_the_host_returned_to_its_pre_run_baseline(evidence) -> None:
    delta = evidence["baseline"]["delta"]
    assert evidence["baseline"]["torn_down"] is True
    for kind in ("containers", "volumes", "networks"):
        assert delta[kind]["leaked"] == [], f"{kind} leaked: {delta[kind]['leaked']}"
        assert delta[kind]["before"] == delta[kind]["after"], delta[kind]


def test_the_image_delta_is_recorded_rather_than_asserted_away(evidence) -> None:
    # `docker compose down` never removes images, by design. Pretending
    # otherwise would be the one dishonest line in the record, so images are
    # tracked separately from the baseline assertion and documented.
    assert "images" in evidence["baseline"]["delta"]
    assert "never removes images" in evidence["notes"]["images"]


def test_the_evidence_record_carries_no_credentials_or_host_paths(evidence) -> None:
    blob = json.dumps(evidence).lower()
    for forbidden in ("secret", "password", "token", "api_key", "/home/", "authorization"):
        assert forbidden not in blob, f"the evidence record leaked {forbidden!r}"


def test_the_recorded_log_is_bounded(evidence) -> None:
    # Details exist to explain a failure, not to carry a payload.
    assert len(evidence["run"].get("observer_log", "")) <= 4000


# ── the compose canary ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token",
    [
        'user: "10001:10001"',
        "read_only: true",
        "no-new-privileges:true",
        "cap_drop",
        "pids_limit",
        "mem_limit",
        "cpus",
        "127.0.0.1:",
        "driver: bridge",
    ],
)
def test_the_compose_file_still_declares_the_posture(compose_text: str, token: str) -> None:
    assert token in compose_text, f"the compose file no longer declares {token!r}"


def test_the_compose_file_mounts_nothing_from_the_host(compose_text: str) -> None:
    for forbidden in ("/var/run/docker.sock", "network_mode: host", "privileged: true", "${HOME}"):
        assert forbidden not in compose_text, forbidden


def test_the_demonstration_is_not_shipped_in_the_wheel() -> None:
    # `demo/` verifies the package the way `tools/` and `cleanroom/` do, and
    # a verifier shipped inside the artifact it verifies is checking itself.
    manifest = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "hb_demo_publisher" not in manifest
    assert "hb_demo_observer" not in manifest
