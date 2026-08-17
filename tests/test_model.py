"""The six-field value type, its canonical encoding, and its identity rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mcp_heartbeat import (
    CORE_FIELDS,
    EXTENSION_VERSION,
    Heartbeat,
    IdentityBinding,
    IdentityClaim,
    InvalidHeartbeat,
    UnsupportedExtensionVersion,
    canonical_json,
    digest_of,
    format_rfc3339,
    parse_rfc3339,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make(**overrides) -> Heartbeat:
    fields = dict(
        node_id="svc/api-7",
        boot_id="3f2a91c0",
        sequence=12,
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=30),
    )
    fields.update(overrides)
    return Heartbeat(**fields)


# ── the contract is six fields ────────────────────────────────────────


def test_the_wire_object_has_exactly_the_six_mandatory_members() -> None:
    assert set(make().to_dict()) == set(CORE_FIELDS)


def test_no_required_member_describes_health_readiness_or_work() -> None:
    # The hard constraint, as a test: these are consumer projections and must
    # never reappear on the wire, whatever a downstream integration wants.
    forbidden = {
        "health",
        "accepting_work",
        "consistency",
        "operational_state",
        "resource_pressure",
        "capabilities_digest",
        "tasks",
    }
    assert forbidden.isdisjoint(make(extensions={"org.x.y": 1}).to_dict())


# ── canonical encoding ────────────────────────────────────────────────


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_two_heartbeats_agreeing_on_the_fields_share_a_digest() -> None:
    assert make().digest == make().digest


def test_any_field_change_changes_the_digest() -> None:
    baseline = make().digest
    assert make(sequence=13).digest != baseline
    assert make(boot_id="other").digest != baseline
    assert make(extensions={"org.x.y": 1}).digest != baseline


def test_digest_is_a_sha256_string() -> None:
    assert digest_of({"a": 1}).startswith("sha256:")
    assert len(digest_of({"a": 1})) == len("sha256:") + 64


# ── timestamps ────────────────────────────────────────────────────────


def test_rfc3339_round_trips_through_utc() -> None:
    assert parse_rfc3339(format_rfc3339(T0)) == T0


def test_a_non_utc_offset_is_normalised_not_rejected() -> None:
    assert parse_rfc3339("2026-01-01T01:00:00+01:00") == T0


def test_a_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_rfc3339("2026-01-01T00:00:00")


# ── round trip ────────────────────────────────────────────────────────


def test_a_heartbeat_survives_a_wire_round_trip() -> None:
    original = make(extensions={"com.example.run_id": "abc"})
    assert Heartbeat.from_dict(original.to_dict()) == original


def test_from_dict_rejects_a_foreign_contract_version() -> None:
    document = make().to_dict() | {"extension_version": "9.9"}
    with pytest.raises(UnsupportedExtensionVersion):
        Heartbeat.from_dict(document)


def test_from_dict_reports_every_violation_not_just_the_first() -> None:
    document = make().to_dict() | {"sequence": -1, "boot_id": "bad/id"}
    with pytest.raises(InvalidHeartbeat) as caught:
        Heartbeat.from_dict(document)
    assert len(caught.value.violations) == 2


# ── revision identity and freshness ───────────────────────────────────


def test_revision_names_the_epoch_and_counter() -> None:
    assert make().revision == "3f2a91c0:12"


def test_freshness_is_evaluated_against_a_supplied_instant() -> None:
    heartbeat = make()
    assert heartbeat.is_fresh(T0)
    assert heartbeat.is_fresh(T0 + timedelta(seconds=29))
    assert not heartbeat.is_fresh(T0 + timedelta(seconds=30))
    assert not heartbeat.is_fresh(T0 + timedelta(seconds=31))


def test_remaining_seconds_goes_negative_past_expiry() -> None:
    assert make().remaining_seconds(T0 + timedelta(seconds=45)) == -15.0


# ── participant / epoch are the normative names for the wire keys ─────


def test_the_normative_aliases_read_the_wire_keys() -> None:
    heartbeat = make()
    assert heartbeat.participant_id == heartbeat.node_id
    assert heartbeat.epoch_id == heartbeat.boot_id


def test_the_wire_keys_are_not_renamed_at_extension_version_0_1() -> None:
    # D-N1: semantics now, spelling at 1.0. A wire rename here would break
    # the archived corpus, so it is a test, not a comment.
    assert EXTENSION_VERSION == "0.1"
    document = make().to_dict()
    assert "node_id" in document and "boot_id" in document
    assert "participant_id" not in document and "epoch_id" not in document


# ── identity is claimed, never proven ─────────────────────────────────


def test_a_parsed_heartbeat_yields_an_unverified_self_claim() -> None:
    claim = make().identity
    assert claim.participant_id == "svc/api-7"
    assert claim.binding is IdentityBinding.UNVERIFIED


def test_a_self_claim_is_never_authenticated_however_it_is_bound() -> None:
    # The core cannot elevate a claim into proof, and an adapter that reports
    # `bound` still has not made the *claim itself* authenticated evidence.
    for binding in IdentityBinding:
        claim = IdentityClaim(participant_id="p", epoch_id="e", binding=binding)
        assert claim.authenticated is False


def test_identity_binding_has_exactly_three_values() -> None:
    # Collapsing these to a boolean is how "the socket was TLS" becomes
    # "this participant proved who it is". Pin the arity.
    assert {b.value for b in IdentityBinding} == {"bound", "unbound", "unverified"}


def test_a_heartbeat_carries_no_authentication_field() -> None:
    assert not {"authenticated", "verified", "principal"} & set(make().to_dict())
