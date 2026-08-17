"""Regenerate the heartbeat conformance corpus.

Run from the package root::

    python tests/fixtures/generate_corpus.py

Each vector is self-describing: a document, whether it is expected to
validate, and — for negative vectors — the exact ``ViolationCode`` a
consumer must name. ``tests/test_corpus.py`` asserts the *complete*
violation set, so a vector that starts failing for a second reason is a
test failure, not a silent pass.

Every timestamp is fixed. Nothing here reads a clock, so the corpus is
byte-stable and a regeneration produces an empty diff unless a vector
genuinely changed.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Fixed epoch for every vector. Deterministic by construction.
T0 = "2026-01-01T00:00:00.000Z"
T30 = "2026-01-01T00:00:30.000Z"
T3600 = "2026-01-01T01:00:00.000Z"
T3601 = "2026-01-01T01:00:01.000Z"


def base(**overrides: object) -> dict[str, object]:
    """The canonical valid document, with ``overrides`` applied."""
    document: dict[str, object] = {
        "extension_version": "0.1",
        "node_id": "svc/api-7",
        "boot_id": "3f2a91c0",
        "sequence": 12,
        "issued_at": T0,
        "expires_at": T30,
    }
    document.update(overrides)
    return document


POSITIVE: list[dict[str, object]] = [
    {
        "name": "minimal",
        "description": "Exactly the six mandatory members and nothing else.",
        "document": base(),
    },
    {
        "name": "sequence-zero",
        "description": "First heartbeat of an epoch; sequence starts at 0.",
        "document": base(sequence=0),
    },
    {
        "name": "window-at-maximum",
        "description": "A window of exactly MAX_LEASE_SECONDS is admissible.",
        "document": base(expires_at=T3600),
    },
    {
        "name": "namespaced-extensions",
        "description": "Optional data under `extensions`; safely ignorable.",
        "document": base(extensions={"com.example.run_id": "abc123"}),
    },
    {
        "name": "namespaced-top-level-member",
        "description": "An unknown but namespaced top-level member is ignored, not rejected.",
        "document": base(**{"org.example.hint": "ignored"}),
    },
]

NEGATIVE: list[dict[str, object]] = [
    {
        "name": "missing-boot-id",
        "description": "Every one of the six members is mandatory.",
        "document": {k: v for k, v in base().items() if k != "boot_id"},
        "reason": "schema_invalid",
        "violations": ["missing required field: boot_id"],
    },
    {
        "name": "unsupported-extension-version",
        "description": "A 0.1 reader refuses a document declaring another contract version.",
        "document": base(extension_version="0.2"),
        "reason": "unsupported_extension_version",
        "violations": ["extension_version must be '0.1', got '0.2'"],
    },
    {
        "name": "sequence-negative",
        "description": "The counter is non-negative.",
        "document": base(sequence=-1),
        "reason": "schema_invalid",
        "violations": ["sequence must be >= 0"],
    },
    {
        "name": "sequence-boolean",
        "description": "`True` is an int in Python; the contract still refuses it.",
        "document": base(sequence=True),
        "reason": "schema_invalid",
        "violations": ["sequence must be an integer"],
    },
    {
        "name": "expires-before-issued",
        "description": "A non-positive window proves nothing.",
        "document": base(issued_at=T30, expires_at=T0),
        "reason": "invalid_expiry_window",
        "violations": ["expires_at must be strictly after issued_at"],
    },
    {
        "name": "window-too-long",
        "description": "A heartbeat is not a lifetime grant.",
        "document": base(expires_at=T3601),
        "reason": "expiry_window_too_long",
        "violations": ["lease window must be <= 3600s"],
    },
    {
        "name": "node-id-malformed",
        "description": "The participant id must match the profile pattern.",
        "document": base(node_id="-leading-dash"),
        "reason": "schema_invalid",
        "violations": [
            "node_id must be a scoped opaque identifier matching the profile pattern"
        ],
    },
    {
        "name": "boot-id-malformed",
        "description": "The epoch id has a narrower alphabet than the participant id.",
        "document": base(boot_id="has/slash"),
        "reason": "schema_invalid",
        "violations": ["boot_id must be an opaque identifier matching the profile pattern"],
    },
    {
        "name": "extension-key-not-namespaced",
        "description": "Optional data must be namespaced so it cannot collide with core fields.",
        "document": base(extensions={"run_id": "abc123"}),
        "reason": "schema_invalid",
        "violations": ["extensions key 'run_id' must be namespaced (e.g. 'com.example')"],
    },
    {
        "name": "unknown-member-not-namespaced",
        "description": "A typo'd core field must not pass as a safely-ignorable extension.",
        "document": base(expires_ats=T30),
        "reason": "schema_invalid",
        "violations": ["unknown member 'expires_ats' must be namespaced to be ignorable"],
    },
    {
        "name": "timestamp-without-offset",
        "description": "Wire instants are explicit UTC; a naive timestamp is ambiguous.",
        "document": base(issued_at="2026-01-01T00:00:00"),
        "reason": "schema_invalid",
        "violations": ["issued_at must be an RFC 3339 UTC timestamp"],
    },
    {
        "name": "timestamp-not-a-string",
        "description": "An epoch-seconds number is not RFC 3339.",
        "document": base(expires_at=1767225600),
        "reason": "schema_invalid",
        "violations": ["expires_at must be an RFC 3339 UTC timestamp"],
    },
]


def main() -> None:
    for kind, vectors in (("positive", POSITIVE), ("negative", NEGATIVE)):
        target = HERE / kind
        target.mkdir(parents=True, exist_ok=True)
        for vector in vectors:
            path = target / f"{vector['name']}.json"
            path.write_text(json.dumps(vector, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(POSITIVE)} positive and {len(NEGATIVE)} negative vectors")


if __name__ == "__main__":
    main()
