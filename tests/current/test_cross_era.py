"""Era negotiation is explicit, observable, and confusable by nothing.

The dangerous failure is not a rejected request — it is one that *both*
eras half-accept. So every test here asks the same question from a
different angle: can a message be constructed that a gateway would forward
to either backend and that neither would reject?
"""
from __future__ import annotations

import pytest

from mcp_heartbeat_current import contract
from mcp_heartbeat_current.discovery import build_discover_request
from mcp_heartbeat_current.era import (
    LEGACY_ONLY_METHODS,
    Era,
    downgrade_refused,
    has_modern_envelope,
    leaked_legacy_headers,
    refuse_on_current_path,
    route,
)
from mcp_heartbeat_current.errors import (
    CrossEraConfusion,
    ForbiddenPrimitiveUsed,
    UnsupportedProtocolVersion,
)
from mcp_heartbeat_current.metadata import build_request


def _legacy_initialize(version: str = "2025-06-18") -> dict:
    """A legacy handshake, written here in the test rather than the package.

    HB-03 is greenfield: nothing in the adapter constructs one of these, so
    the fixture for "what a legacy client sends" lives with the test that
    proves it is refused.
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {"experimental": {"presenceLease": {"version": "0.1"}}},
            "clientInfo": {"name": "legacy", "version": "0.1.0"},
        },
    }


# ── classification is explicit and observable ─────────────────────────


def test_a_modern_request_routes_current_on_stated_evidence() -> None:
    body, headers = build_discover_request(request_id=1)
    routed = route(body, headers)
    assert routed.era is Era.CURRENT
    assert routed.protocol_version == "2026-07-28"
    assert routed.evidence == (
        f"_meta[{contract.PROTOCOL_VERSION_META_KEY}]=2026-07-28",
    ), "an operator can see why, not just what"


def test_a_legacy_handshake_routes_handshake_and_is_never_answered_modern() -> None:
    routed = route(_legacy_initialize())
    assert routed.era is Era.HANDSHAKE
    assert routed.protocol_version == "2025-06-18"
    assert routed.modern is None


def test_the_discriminator_is_one_checkable_key_not_a_heuristic() -> None:
    body, _ = build_discover_request(request_id=1)
    assert has_modern_envelope(body)
    assert not has_modern_envelope(_legacy_initialize())
    assert not has_modern_envelope({"method": "resources/read", "params": {"uri": "x"}})


# ── the confusable messages ───────────────────────────────────────────


def test_a_handshake_carrying_a_modern_envelope_is_refused() -> None:
    """The worst message in the world: plausible to both backends."""
    body = _legacy_initialize()
    body["params"]["_meta"] = {contract.PROTOCOL_VERSION_META_KEY: "2026-07-28"}
    with pytest.raises(CrossEraConfusion) as excinfo:
        route(body)
    assert "modern _meta envelope" in str(excinfo.value)


def test_a_handshake_proposing_a_modern_revision_is_refused() -> None:
    """2026-07-28 has no handshake, so this can only be a confused peer."""
    with pytest.raises(CrossEraConfusion) as excinfo:
        route(_legacy_initialize(version="2026-07-28"))
    assert "no handshake" in str(excinfo.value)


def test_a_modern_envelope_on_a_legacy_only_method_is_refused() -> None:
    body, headers = build_request(
        "resources/subscribe", {"uri": "heartbeat://svc/api-7"}, request_id=2
    )
    with pytest.raises(CrossEraConfusion) as excinfo:
        route(body, headers)
    assert "does not exist on the modern path" in str(excinfo.value)


def test_a_message_belonging_to_neither_era_is_refused() -> None:
    with pytest.raises(CrossEraConfusion):
        route({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "x"}})


def test_a_modern_header_without_a_modern_body_is_refused() -> None:
    """A gateway that stamps the header but strips ``_meta``."""
    with pytest.raises(CrossEraConfusion) as excinfo:
        route(
            {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "x"}},
            {contract.MCP_PROTOCOL_VERSION_HEADER: "2026-07-28"},
        )
    assert contract.MCP_PROTOCOL_VERSION_HEADER in str(excinfo.value)


def test_a_message_without_a_method_belongs_to_no_era() -> None:
    with pytest.raises(CrossEraConfusion):
        route({"jsonrpc": "2.0", "id": 1})


# ── downgrade ─────────────────────────────────────────────────────────


def test_a_legacy_only_peer_is_a_refused_downgrade_not_a_fallback() -> None:
    """Correct and expected — the eras are not interchangeable."""
    assert downgrade_refused(["2025-06-18", "2025-11-25"])
    assert not downgrade_refused(["2026-07-28"])
    assert not downgrade_refused(["2025-06-18", "2026-07-28"])
    assert downgrade_refused(None)


def test_negotiating_with_a_legacy_only_server_raises_rather_than_downgrades() -> None:
    from mcp_heartbeat_current.discovery import negotiate

    with pytest.raises(UnsupportedProtocolVersion):
        negotiate({"supportedVersions": ["2025-06-18"], "capabilities": {}})


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_every_handshake_revision_is_refused_on_the_modern_path(version: str) -> None:
    body, headers = build_request("resources/read", {"uri": "heartbeat://a"}, request_id=1)
    body["params"]["_meta"][contract.PROTOCOL_VERSION_META_KEY] = version
    headers[contract.MCP_PROTOCOL_VERSION_HEADER] = version
    with pytest.raises(UnsupportedProtocolVersion):
        route(body, headers)


# ── one endpoint, two eras, no leakage ────────────────────────────────


def test_one_endpoint_can_classify_both_eras_without_mixing_them() -> None:
    """The PRD's "one server endpoint may support both eras" case.

    Interleaving the two message kinds through a single classifier changes
    nothing about either verdict: there is no shared mutable state for one
    era's traffic to leak into the other's.
    """
    modern_body, modern_headers = build_discover_request(request_id=1)
    legacy_body = _legacy_initialize()

    verdicts = [
        route(modern_body, modern_headers).era,
        route(legacy_body).era,
        route(modern_body, modern_headers).era,
        route(legacy_body).era,
    ]
    assert verdicts == [Era.CURRENT, Era.HANDSHAKE, Era.CURRENT, Era.HANDSHAKE]


def test_a_legacy_session_header_on_a_modern_request_is_visible_not_honoured() -> None:
    body, headers = build_discover_request(request_id=1)
    headers["Mcp-Session-Id"] = "sess-1"
    routed = route(body, headers)
    assert routed.era is Era.CURRENT
    assert routed.to_dict()["ignored_headers"] == ["mcp-session-id"]
    assert leaked_legacy_headers(headers) == ("mcp-session-id",)


def test_the_legacy_capability_key_never_appears_in_a_modern_result() -> None:
    """D-07: ``experimental.presenceLease`` is not a conformant modern id."""
    from mcp_heartbeat_current.discovery import build_discover_result

    result = build_discover_result(server_info={"name": "lab", "version": "1"})
    assert "experimental" not in result["capabilities"]
    assert "presenceLease" not in str(result)


# ── explicit refusal helper ───────────────────────────────────────────


@pytest.mark.parametrize("method", LEGACY_ONLY_METHODS)
def test_every_legacy_only_method_is_refused_by_name(method: str) -> None:
    with pytest.raises(ForbiddenPrimitiveUsed):
        refuse_on_current_path({"method": method})


def test_a_modern_method_passes_the_refusal_check() -> None:
    refuse_on_current_path({"method": "server/discover"})
    refuse_on_current_path({"method": "subscriptions/listen"})
    refuse_on_current_path({"method": "resources/read"})
