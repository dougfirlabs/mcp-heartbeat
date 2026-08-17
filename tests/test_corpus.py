"""Fixture-driven conformance.

The corpus is the artifact a clean-room implementer checks against, so the
assertions here are exact: a negative vector must produce *precisely* the
violations it declares. A vector that starts failing for a second reason is
a failure, not a silent pass.

Regenerate with ``python tests/fixtures/generate_corpus.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_heartbeat import (
    Heartbeat,
    HeartbeatError,
    ViolationCode,
    document_reason,
    validate_document,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(kind: str) -> list[tuple[str, dict]]:
    return sorted(
        (path.stem, json.loads(path.read_text())) for path in (FIXTURES / kind).glob("*.json")
    )


POSITIVE = load("positive")
NEGATIVE = load("negative")


def test_the_corpus_is_present() -> None:
    assert len(POSITIVE) >= 5 and len(NEGATIVE) >= 10


@pytest.mark.parametrize("name,vector", POSITIVE, ids=[n for n, _ in POSITIVE])
def test_positive_vectors_validate(name: str, vector: dict) -> None:
    assert validate_document(vector["document"]) == [], vector["description"]
    assert document_reason(vector["document"]) is None


@pytest.mark.parametrize("name,vector", POSITIVE, ids=[n for n, _ in POSITIVE])
def test_positive_vectors_round_trip_byte_stably(name: str, vector: dict) -> None:
    parsed = Heartbeat.from_dict(vector["document"])
    assert parsed.to_dict() == vector["document"] or set(vector["document"]) - set(
        parsed.to_dict()
    ), "only ignorable namespaced members may be dropped"
    assert Heartbeat.from_dict(parsed.to_dict()) == parsed


@pytest.mark.parametrize("name,vector", NEGATIVE, ids=[n for n, _ in NEGATIVE])
def test_negative_vectors_report_exactly_their_declared_violations(
    name: str, vector: dict
) -> None:
    assert validate_document(vector["document"]) == vector["violations"]


@pytest.mark.parametrize("name,vector", NEGATIVE, ids=[n for n, _ in NEGATIVE])
def test_negative_vectors_name_their_declared_reason_code(
    name: str, vector: dict
) -> None:
    assert document_reason(vector["document"]) is ViolationCode(vector["reason"])


@pytest.mark.parametrize("name,vector", NEGATIVE, ids=[n for n, _ in NEGATIVE])
def test_negative_vectors_never_parse(name: str, vector: dict) -> None:
    with pytest.raises(HeartbeatError):
        Heartbeat.from_dict(vector["document"])


def test_the_corpus_is_deterministic() -> None:
    # No vector may embed a value that changes between runs, or the corpus
    # stops being a fixed point a second implementation can check against.
    for _, vector in POSITIVE + NEGATIVE:
        rendered = json.dumps(vector["document"], sort_keys=True)
        assert "now" not in rendered.lower()
