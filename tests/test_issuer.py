"""Issuance: ordered counters, minted epochs, and no hidden clock reads."""
from __future__ import annotations

import re

from mcp_heartbeat import (
    DEFAULT_LEASE_SECONDS,
    FakeClock,
    HeartbeatIssuer,
    mint_epoch_id,
    validate_document,
)


def test_the_counter_starts_at_zero_and_rises_by_one() -> None:
    issuer = HeartbeatIssuer(participant_id="p", epoch_id="e", clock=FakeClock())
    assert [issuer.issue().sequence for _ in range(4)] == [0, 1, 2, 3]


def test_every_issued_heartbeat_validates() -> None:
    issuer = HeartbeatIssuer(participant_id="svc/api-7", epoch_id="e1", clock=FakeClock())
    assert validate_document(issuer.issue().to_dict()) == []


def test_the_window_follows_the_configured_lease() -> None:
    clock = FakeClock()
    issuer = HeartbeatIssuer(
        participant_id="p", epoch_id="e", clock=clock, lease_seconds=45
    )
    heartbeat = issuer.issue()
    assert heartbeat.issued_at == clock.now()
    assert heartbeat.remaining_seconds(clock.now()) == 45.0


def test_issuance_reads_only_the_injected_clock() -> None:
    # Two issues at the same fake instant carry the same timestamps; a real
    # clock read anywhere in the path would make these differ.
    issuer = HeartbeatIssuer(participant_id="p", epoch_id="e", clock=FakeClock())
    first, second = issuer.issue(), issuer.issue()
    assert first.issued_at == second.issued_at
    assert first.expires_at == second.expires_at
    assert first.digest != second.digest  # only the counter moved


def test_the_default_lease_is_short_enough_to_be_a_heartbeat() -> None:
    assert 0 < DEFAULT_LEASE_SECONDS <= 60


# ── epochs are minted, never derived ──────────────────────────────────


def test_a_minted_epoch_is_random_not_derived() -> None:
    # A derived epoch re-presents a retired id on restart, which a consumer
    # correctly classifies as replay. Distinctness is the whole defence.
    minted = {mint_epoch_id() for _ in range(64)}
    assert len(minted) == 64


def test_a_minted_epoch_matches_the_wire_pattern() -> None:
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", mint_epoch_id())


def test_an_issuer_without_an_epoch_mints_one() -> None:
    first = HeartbeatIssuer(participant_id="p", clock=FakeClock())
    second = HeartbeatIssuer(participant_id="p", clock=FakeClock())
    assert first.epoch_id != second.epoch_id


def test_the_counter_never_resets_within_an_issuer() -> None:
    # "A new epoch means a new issuer" is the same rule as "a new process
    # means a new epoch" — there is deliberately no reset() to call.
    issuer = HeartbeatIssuer(participant_id="p", epoch_id="e", clock=FakeClock())
    for _ in range(3):
        issuer.issue()
    assert issuer.next_sequence == 3
    assert not hasattr(issuer, "reset")


def test_extensions_are_carried_onto_every_heartbeat() -> None:
    issuer = HeartbeatIssuer(
        participant_id="p",
        epoch_id="e",
        clock=FakeClock(),
        extensions={"org.example.k": "v"},
    )
    assert issuer.issue().to_dict()["extensions"] == {"org.example.k": "v"}
