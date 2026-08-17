"""The repaired legacy corpus, executed.

``corpus/legacy-repaired-1.json`` is a *separately versioned* set of
expectations for the repaired adapter. It does not replace the HB-00
historical corpus and it is not derived from it: the archived expectations
describe the originating integration lab as it stands, and the epic forbids
editing them. Both corpora run independently, and
``test_defect_closure.py`` proves the historical one is untouched.

Every vector names the defect it closes and its polarity, so the closure
matrix in that module is computed from the corpus rather than asserted
alongside it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcp_heartbeat.model import EXTENSION_VERSION
from mcp_heartbeat_legacy import (
    HEARTBEAT_CAPABILITY,
    HEARTBEAT_NAMESPACE,
    LEGACY_ERA,
    LegacyProtocolError,
    LegacyServerSession,
    advertise,
    agreement_violations,
    negotiate_heartbeat,
    negotiate_protocol,
    parse_updated,
)

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
CORPUS_PATH = CORPUS_DIR / "legacy-repaired-1.json"
CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
VECTORS: list[dict[str, Any]] = CORPUS["vectors"]

_MISSING = object()


def _ids(vectors: list[dict[str, Any]]) -> list[str]:
    return [vector["id"] for vector in vectors]


def by_kind(kind: str) -> list[dict[str, Any]]:
    return [vector for vector in VECTORS if vector["kind"] == kind]


# ── the corpus is well formed ─────────────────────────────────────────


def test_the_corpus_declares_its_own_version_and_its_two_axes() -> None:
    assert CORPUS["corpus_version"] == "legacy-repaired-1"
    assert CORPUS["mcp_protocol_era"] == LEGACY_ERA
    assert CORPUS["extension_version"] == EXTENSION_VERSION
    # The two axes are named apart in the corpus itself, not merged into one
    # "version" string — the same separation the adapter reports at runtime.
    assert CORPUS["mcp_protocol_era"] != CORPUS["extension_version"]


def test_every_vector_id_is_unique() -> None:
    ids = _ids(VECTORS)
    assert len(ids) == len(set(ids))


# ── D-04 · protocol-version negotiation ───────────────────────────────


@pytest.mark.parametrize("vector", by_kind("protocol_negotiation"), ids=_ids(by_kind("protocol_negotiation")))
def test_protocol_negotiation_vector(vector: dict[str, Any]) -> None:
    requested = vector["input"].get("requested", _MISSING)
    result = (
        negotiate_protocol() if requested is _MISSING else negotiate_protocol(requested)
    )
    expect = vector["expect"]

    assert result.outcome.value == expect["outcome"]
    assert result.code.value == expect["code"]
    assert result.negotiated == expect["negotiated"]
    assert result.disagreement is expect["disagreement"]


# ── heartbeat capability negotiation ──────────────────────────────────


@pytest.mark.parametrize("vector", by_kind("heartbeat_negotiation"), ids=_ids(by_kind("heartbeat_negotiation")))
def test_heartbeat_negotiation_vector(vector: dict[str, Any]) -> None:
    result = negotiate_heartbeat(vector["input"]["capabilities"])
    expect = vector["expect"]

    assert result.outcome.value == expect["outcome"]
    assert result.code.value == expect["code"]
    assert result.supported is expect["supported"]
    assert result.extension_version == expect["extension_version"]


# ── D-02 · advertisement / registry agreement ─────────────────────────


@pytest.mark.parametrize("vector", by_kind("capability_agreement"), ids=_ids(by_kind("capability_agreement")))
def test_capability_agreement_vector(vector: dict[str, Any]) -> None:
    data = vector["input"]
    implemented = data["implemented"]

    if data["mode"] == "derive":
        advertised = advertise(implemented)
        assert advertised["resources"]["subscribe"] is vector["expect"]["subscribe_advertised"]
    else:
        advertised = data["advertised"]

    violations = agreement_violations(advertised, implemented)
    expected = vector["expect"]["violations"]

    if not expected:
        assert violations == []
    else:
        for fragment in expected:
            assert any(fragment in violation for violation in violations), (
                f"expected a violation mentioning {fragment!r}, got {violations}"
            )


# ── D-03 · the lifecycle, both directions ─────────────────────────────


@pytest.mark.parametrize("vector", by_kind("handshake"), ids=_ids(by_kind("handshake")))
def test_handshake_vector(vector: dict[str, Any]) -> None:
    data = vector["input"]
    implemented = list(data["implemented"])
    server = LegacyServerSession(
        server_name="corpus",
        methods={name: (lambda params: {"served": True}) for name in implemented},
    )

    for step in data["steps"]:
        params = _params_for(step["method"], data)
        if step["expect"] == "ok":
            server.handle(step["method"], params)
            continue
        with pytest.raises(LegacyProtocolError) as caught:
            server.handle(step["method"], params)
        assert caught.value.reason.value == step["reason"]

    expect = vector["expect"]
    assert server.state.value == expect["final_state"]
    assert server.heartbeat_ready is expect["heartbeat_ready"]
    if "disagreements" in expect:
        assert len(server.disagreements) == expect["disagreements"]
        # D-04's other half: the ledger is reachable from stats, which is
        # what the archived server had no equivalent of.
        assert server.stats()["protocol_version_disagreements"]


def _params_for(method: str, data: dict[str, Any]) -> dict[str, Any]:
    if method != "initialize":
        return {"uri": "presence://acme/worker-1"}
    return {
        "protocolVersion": data.get("protocol_version", LEGACY_ERA),
        "capabilities": {
            HEARTBEAT_NAMESPACE: {
                HEARTBEAT_CAPABILITY: {"extension_version": EXTENSION_VERSION}
            }
        },
        "clientInfo": {"name": "corpus", "version": "0.1.0"},
    }


# ── D-10 · change-hint parsing ────────────────────────────────────────


@pytest.mark.parametrize("vector", by_kind("updated_parse"), ids=_ids(by_kind("updated_parse")))
def test_updated_parse_vector(vector: dict[str, Any]) -> None:
    data = vector["input"]
    expect = vector["expect"]

    if "error" in expect:
        with pytest.raises(LegacyProtocolError) as caught:
            parse_updated(data["method"], data["params"])
        assert caught.value.reason.value == expect["error"]
        return

    hint = parse_updated(data["method"], data["params"])
    assert hint.address == expect["address"]
    assert hint.revision == expect["revision"]
    assert hint.digest == expect["digest"]
    assert hint.carries_revision_metadata is expect["carries_revision_metadata"]
    assert hint.overloaded_standard_method is expect["overloaded_standard_method"]
