"""The stdlib validator: complete violation sets and specific reason codes."""
from __future__ import annotations

import pytest

from mcp_heartbeat import (
    MAX_LEASE_SECONDS,
    REQUIRED_FIELDS,
    ViolationCode,
    document_reason,
    expiry_window_violation,
    validate_document,
)

VALID = {
    "extension_version": "0.1",
    "node_id": "svc/api-7",
    "boot_id": "3f2a91c0",
    "sequence": 12,
    "issued_at": "2026-01-01T00:00:00.000Z",
    "expires_at": "2026-01-01T00:00:30.000Z",
}


def test_the_required_set_is_exactly_six_fields() -> None:
    assert REQUIRED_FIELDS == (
        "extension_version",
        "node_id",
        "boot_id",
        "sequence",
        "issued_at",
        "expires_at",
    )


def test_the_canonical_document_validates() -> None:
    assert validate_document(VALID) == []


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_required_field_is_required(field: str) -> None:
    document = {k: v for k, v in VALID.items() if k != field}
    assert f"missing required field: {field}" in validate_document(document)


def test_a_non_object_is_rejected_without_crashing() -> None:
    assert validate_document(["not", "an", "object"]) == ["document must be a JSON object"]


def test_violations_accumulate_rather_than_short_circuiting() -> None:
    document = VALID | {"sequence": -1, "node_id": "-bad", "boot_id": "bad/id"}
    assert len(validate_document(document)) == 3


# ── optional data ─────────────────────────────────────────────────────


def test_namespaced_extensions_are_accepted() -> None:
    assert validate_document(VALID | {"extensions": {"com.example.run": "r"}}) == []


def test_an_unnamespaced_extension_key_is_rejected() -> None:
    violations = validate_document(VALID | {"extensions": {"run": "r"}})
    assert violations == ["extensions key 'run' must be namespaced (e.g. 'com.example')"]


def test_a_namespaced_top_level_member_is_ignorable() -> None:
    assert validate_document(VALID | {"com.example.anything": {"deep": 1}}) == []


def test_an_unnamespaced_unknown_member_is_rejected() -> None:
    # Otherwise a typo'd core field silently passes as an extension.
    violations = validate_document(VALID | {"expires_ats": "2026-01-01T00:00:30.000Z"})
    assert violations == ["unknown member 'expires_ats' must be namespaced to be ignorable"]


# ── the expiry window ─────────────────────────────────────────────────


def test_a_window_of_exactly_the_maximum_is_admissible() -> None:
    assert expiry_window_violation(VALID | {"expires_at": "2026-01-01T01:00:00.000Z"}) is None
    assert MAX_LEASE_SECONDS == 3600.0


def test_a_window_beyond_the_maximum_names_its_own_code() -> None:
    document = VALID | {"expires_at": "2026-01-01T01:00:01.000Z"}
    assert expiry_window_violation(document) is ViolationCode.EXPIRY_WINDOW_TOO_LONG
    assert document_reason(document) is ViolationCode.EXPIRY_WINDOW_TOO_LONG


def test_a_non_positive_window_names_its_own_code() -> None:
    document = VALID | {"expires_at": VALID["issued_at"]}
    assert document_reason(document) is ViolationCode.INVALID_EXPIRY_WINDOW


def test_an_unparseable_window_is_not_reported_as_a_window_problem() -> None:
    assert expiry_window_violation(VALID | {"issued_at": "nonsense"}) is None


# ── reason codes ──────────────────────────────────────────────────────


def test_a_valid_document_has_no_reason() -> None:
    assert document_reason(VALID) is None


def test_an_unreadable_version_outranks_missing_field_noise() -> None:
    assert document_reason({"extension_version": "0.2"}) is (
        ViolationCode.UNSUPPORTED_EXTENSION_VERSION
    )


def test_a_missing_version_is_an_unsupported_version() -> None:
    assert document_reason({}) is ViolationCode.UNSUPPORTED_EXTENSION_VERSION


def test_structural_problems_fall_through_to_schema_invalid() -> None:
    assert document_reason(VALID | {"sequence": "twelve"}) is ViolationCode.SCHEMA_INVALID


def test_reason_codes_are_stable_strings() -> None:
    # Consumers key on these; they are wire vocabulary, not display text.
    assert ViolationCode.BOOT_ID_REUSE.value == "boot_id_reuse"
    assert ViolationCode.NODE_ID_MISMATCH.value == "node_id_mismatch"
    assert str(ViolationCode.SEQUENCE_CONFLICT) == "sequence_conflict"
