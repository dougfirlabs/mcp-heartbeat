"""Discovery and extension negotiation — MCP-HB-03-S1."""
from __future__ import annotations

import pytest

from mcp_heartbeat_current import contract
from mcp_heartbeat_current.discovery import (
    HeartbeatCapability,
    build_discover_request,
    build_discover_result,
    build_read_request,
    negotiate,
    serves_change_hints,
)
from mcp_heartbeat_current.errors import (
    UnsupportedHeartbeatExtension,
    UnsupportedProtocolVersion,
)
from mcp_heartbeat_current.metadata import classify_request


def _result(**kwargs):
    kwargs.setdefault("server_info", {"name": "lab", "version": "0.1.0"})
    return build_discover_result(**kwargs)


# ── discovery replaces the handshake ──────────────────────────────────


def test_discovery_is_one_request_with_no_lifecycle_exchange() -> None:
    """The acceptance criterion, stated as a test.

    A conformant modern client learns everything from ``server/discover``.
    There is no second request, and nothing in the exchange is a lifecycle
    method.
    """
    body, headers = build_discover_request(request_id=1)
    assert body["method"] == "server/discover"
    route = classify_request(body, headers)
    assert route.protocol_version == "2026-07-28"
    assert route.method == "server/discover"


def test_the_discover_result_advertises_the_prefixed_extension() -> None:
    result = _result()
    extensions = result["capabilities"]["extensions"]
    assert contract.HEARTBEAT_EXTENSION_ID in extensions
    assert extensions[contract.HEARTBEAT_EXTENSION_ID]["extension_version"] == "0.1"
    assert result["supportedVersions"] == ["2026-07-28"]


def test_both_versions_are_independently_visible_in_the_result() -> None:
    """Acceptance: protocol revision and extension version, side by side."""
    result = _result()
    revision = result["supportedVersions"]
    extension_version = result["capabilities"]["extensions"][contract.HEARTBEAT_EXTENSION_ID][
        "extension_version"
    ]
    assert revision == ["2026-07-28"]
    assert extension_version == "0.1"
    assert extension_version not in revision


def test_the_negotiation_reports_both_axes_separately() -> None:
    negotiated = negotiate(_result())
    assert negotiated.protocol_revision == "2026-07-28"
    assert negotiated.extension_version == "0.1"
    assert negotiated.heartbeat_enabled
    assert negotiated.to_dict()["extension_id"] == contract.HEARTBEAT_EXTENSION_ID


def test_change_hint_capability_is_derived_from_what_the_server_serves() -> None:
    """A capability claiming a stream the server won't open is worse than none."""
    serving = _result(serves_listen=True)
    assert serving["capabilities"]["resources"] == {"subscribe": True, "listChanged": True}

    silent = _result(serves_listen=False, capability=HeartbeatCapability(change_hints=False))
    assert silent["capabilities"]["resources"] == {"subscribe": False, "listChanged": False}
    assert not serves_change_hints(negotiate(silent))


# ── failure is scoped ─────────────────────────────────────────────────


def test_no_mutual_revision_is_fatal() -> None:
    """Without a shared revision there is no conversation to scope down to."""
    with pytest.raises(UnsupportedProtocolVersion):
        negotiate({"supportedVersions": ["2025-06-18"], "capabilities": {}})


def test_an_unreadable_extension_version_disables_only_heartbeat() -> None:
    """Acceptance: unsupported versions fail clearly without disabling
    unrelated MCP features."""
    result = _result(
        capability=HeartbeatCapability(extension_version="9.9"),
        other_capabilities={"tools": {"listChanged": True}, "prompts": {}},
    )
    negotiated = negotiate(result)

    assert negotiated.protocol_revision == "2026-07-28", "the MCP session is fine"
    assert not negotiated.heartbeat_enabled
    assert negotiated.disabled_reason == "unsupported_extension_version"
    assert negotiated.extension_version == "9.9"
    assert negotiated.other_capabilities["tools"] == {"listChanged": True}
    assert "prompts" in negotiated.other_capabilities


def test_a_server_without_heartbeat_is_still_a_usable_server() -> None:
    result = {
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {"listChanged": True}, "extensions": {"io.other/thing": {}}},
    }
    negotiated = negotiate(result)
    assert negotiated.protocol_revision == "2026-07-28"
    assert not negotiated.heartbeat_enabled
    assert negotiated.disabled_reason == "extension_absent"
    assert negotiated.other_capabilities["extensions"] == {"io.other/thing": {}}


def test_strict_callers_may_opt_into_raising() -> None:
    result = _result(capability=HeartbeatCapability(extension_version="9.9"))
    with pytest.raises(UnsupportedHeartbeatExtension) as excinfo:
        negotiate(result, strict_extension=True)
    assert excinfo.value.code == contract.METHOD_NOT_FOUND
    assert excinfo.value.to_error()["data"]["extension"] == contract.HEARTBEAT_EXTENSION_ID


def test_a_malformed_extension_block_disables_heartbeat_without_raising() -> None:
    result = {
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"extensions": {contract.HEARTBEAT_EXTENSION_ID: "not-an-object"}},
    }
    assert negotiate(result).disabled_reason == "extension_absent"


# ── the authoritative fetch ───────────────────────────────────────────


def test_the_read_request_addresses_the_participant_and_mirrors_its_uri() -> None:
    negotiated = negotiate(_result())
    body, headers = build_read_request("svc/api-7", negotiated, request_id=7)
    assert body["method"] == "resources/read"
    assert body["params"]["uri"] == "heartbeat://participants/svc%2Fapi-7"
    assert headers[contract.MCP_NAME_HEADER] == body["params"]["uri"]
    assert classify_request(body, headers).method == "resources/read"


def test_one_server_addresses_many_participants() -> None:
    capability = HeartbeatCapability()
    assert capability.resource_uri("a/1") != capability.resource_uri("a/2")


@pytest.mark.parametrize(
    "participant_id",
    ["svc/api-7", "plain", "ns:svc/a+b", "a.b_c-d", "deep/nested/path"],
)
def test_a_participant_id_survives_the_uri_round_trip(participant_id: str) -> None:
    """Ids may legally contain ``/``, ``:`` and ``+`` — all URI-significant.

    Unencoded, ``svc/api-7`` becomes two path segments and stops matching
    the resource template, which is how a real SDK read fails with "unknown
    resource" for a participant that is perfectly well published.
    """
    capability = HeartbeatCapability()
    uri = capability.resource_uri(participant_id)
    segment = uri.rsplit("/", 1)[-1]
    assert "/" not in segment
    assert capability.participant_from_uri_segment(segment) == participant_id


def test_reading_through_a_disabled_negotiation_is_refused() -> None:
    disabled = negotiate(_result(capability=HeartbeatCapability(extension_version="9.9")))
    with pytest.raises(UnsupportedHeartbeatExtension):
        build_read_request("svc/api-7", disabled, request_id=1)
