"""Per-request metadata, the header ladder, and the transport-level guards."""
from __future__ import annotations

import copy

import pytest

from mcp_heartbeat_current import contract
from mcp_heartbeat_current.errors import (
    HeaderMismatch,
    MalformedEnvelope,
    UnsupportedProtocolVersion,
)
from mcp_heartbeat_current.metadata import (
    RequestEnvelope,
    build_request,
    classify_request,
    http_method_allowed,
    origin_allowed,
    response_headers,
)


def _read_request():
    return build_request(
        "resources/read",
        {"uri": "heartbeat://svc/api-7"},
        request_id=1,
        client_info={"name": "consumer", "version": "0.1.0"},
        client_capabilities={"extensions": {}},
    )


# ── outbound ──────────────────────────────────────────────────────────


def test_every_request_carries_the_envelope_because_there_is_no_session() -> None:
    body, _ = _read_request()
    meta = body["params"]["_meta"]
    assert meta[contract.PROTOCOL_VERSION_META_KEY] == "2026-07-28"
    assert meta[contract.CLIENT_INFO_META_KEY] == {"name": "consumer", "version": "0.1.0"}
    assert contract.CLIENT_CAPABILITIES_META_KEY in meta


def test_headers_mirror_the_body() -> None:
    body, headers = _read_request()
    assert headers[contract.MCP_PROTOCOL_VERSION_HEADER] == "2026-07-28"
    assert headers[contract.MCP_METHOD_HEADER] == body["method"]
    assert headers[contract.MCP_NAME_HEADER] == body["params"]["uri"]


def test_mcp_name_appears_only_for_name_bearing_methods() -> None:
    """Emitting it elsewhere would make the header meaningless."""
    _, discover_headers = build_request("server/discover", {}, request_id=1)
    assert contract.MCP_NAME_HEADER not in discover_headers

    _, tool_headers = build_request("tools/call", {"name": "ping"}, request_id=2)
    assert tool_headers[contract.MCP_NAME_HEADER] == "ping"


def test_a_name_bearing_method_without_its_name_param_is_refused_locally() -> None:
    """Caught before the wire: a strict gateway would 400 this anyway."""
    with pytest.raises(MalformedEnvelope):
        RequestEnvelope(method="resources/read", params={}).headers()


def test_the_envelope_never_mints_a_session_id() -> None:
    _, headers = _read_request()
    assert not any("session" in key.lower() for key in headers)


def test_caller_supplied_meta_is_preserved_alongside_the_envelope() -> None:
    body, _ = build_request(
        "resources/read",
        {"uri": "heartbeat://a", "_meta": {"com.example/trace": "t-1"}},
        request_id=3,
    )
    meta = body["params"]["_meta"]
    assert meta["com.example/trace"] == "t-1"
    assert meta[contract.PROTOCOL_VERSION_META_KEY] == "2026-07-28"


# ── the inbound ladder ────────────────────────────────────────────────


def test_a_conformant_request_clears_all_three_rungs() -> None:
    body, headers = _read_request()
    route = classify_request(body, headers)
    assert route.protocol_version == "2026-07-28"
    assert route.method == "resources/read"
    assert route.ignored_headers == ()


def test_rung_one_missing_envelope_is_invalid_params() -> None:
    body, headers = _read_request()
    del body["params"]["_meta"]
    with pytest.raises(MalformedEnvelope) as excinfo:
        classify_request(body, headers)
    assert excinfo.value.code == contract.INVALID_PARAMS


def test_rung_one_runs_before_rung_two() -> None:
    """Order is normative.

    A body with no envelope also disagrees with its headers. Checking
    headers first would report a mismatch against a value the body never
    declared, sending an operator hunting for a proxy that rewrote nothing.
    """
    body, headers = _read_request()
    del body["params"]["_meta"]
    headers[contract.MCP_METHOD_HEADER] = "tools/call"  # also wrong
    with pytest.raises(MalformedEnvelope):
        classify_request(body, headers)


@pytest.mark.parametrize(
    "header,value",
    [
        (contract.MCP_PROTOCOL_VERSION_HEADER, "2025-06-18"),
        (contract.MCP_METHOD_HEADER, "tools/call"),
        (contract.MCP_NAME_HEADER, "heartbeat://someone-else"),
    ],
)
def test_rung_two_any_header_disagreement_is_header_mismatch(header: str, value: str) -> None:
    body, headers = _read_request()
    headers[header] = value
    with pytest.raises(HeaderMismatch) as excinfo:
        classify_request(body, headers)
    assert excinfo.value.code == -32020
    assert contract.ERROR_CODE_HTTP_STATUS[excinfo.value.code] == 400


def test_rung_two_compares_exactly_not_approximately() -> None:
    """A header that is "close enough" is a routing decision on an unverified value."""
    body, headers = _read_request()
    headers[contract.MCP_NAME_HEADER] = body["params"]["uri"] + "/"
    with pytest.raises(HeaderMismatch):
        classify_request(body, headers)


def test_header_matching_is_case_insensitive_on_the_header_name() -> None:
    body, headers = _read_request()
    shouty = {key.upper(): value for key, value in headers.items()}
    assert classify_request(body, shouty).method == "resources/read"


def test_rung_three_unsupported_version_names_what_is_supported() -> None:
    body, headers = _read_request()
    body["params"]["_meta"][contract.PROTOCOL_VERSION_META_KEY] = "2027-01-01"
    headers[contract.MCP_PROTOCOL_VERSION_HEADER] = "2027-01-01"
    with pytest.raises(UnsupportedProtocolVersion) as excinfo:
        classify_request(body, headers)
    assert excinfo.value.code == -32022
    assert excinfo.value.to_error()["data"]["supported"] == ["2026-07-28"]


def test_a_legacy_revision_is_refused_on_the_modern_path() -> None:
    body, headers = _read_request()
    body["params"]["_meta"][contract.PROTOCOL_VERSION_META_KEY] = "2025-06-18"
    headers[contract.MCP_PROTOCOL_VERSION_HEADER] = "2025-06-18"
    with pytest.raises(UnsupportedProtocolVersion):
        classify_request(body, headers)


# ── legacy headers: ignored, recorded, never echoed ───────────────────


def test_a_session_id_from_a_gateway_is_ignored_and_recorded() -> None:
    """Conformant behaviour is to ignore it; recording is what makes it visible."""
    body, headers = _read_request()
    headers["Mcp-Session-Id"] = "sess-legacy-1"
    headers["Last-Event-ID"] = "42"
    route = classify_request(body, headers)
    assert set(route.ignored_headers) == {"mcp-session-id", "last-event-id"}


def test_the_response_never_echoes_a_session_id() -> None:
    body, headers = _read_request()
    headers["Mcp-Session-Id"] = "sess-legacy-1"
    out = response_headers(classify_request(body, headers))
    assert out == {contract.MCP_PROTOCOL_VERSION_HEADER: "2026-07-28"}
    assert not any("session" in key.lower() for key in out)


def test_classification_does_not_mutate_the_request() -> None:
    body, headers = _read_request()
    before = copy.deepcopy(body)
    classify_request(body, headers)
    assert body == before


# ── transport guards ──────────────────────────────────────────────────


def test_only_post_reaches_the_modern_endpoint() -> None:
    """GET opened the removed stream; DELETE terminated the removed session."""
    assert http_method_allowed("POST")
    assert http_method_allowed("post")
    assert not http_method_allowed("GET")
    assert not http_method_allowed("DELETE")


def test_origin_is_validated_when_present_and_permitted_when_absent() -> None:
    allowed = ["https://console.example"]
    assert origin_allowed(None, allowed), "a non-browser client sends no Origin"
    assert origin_allowed("https://console.example", allowed)
    assert not origin_allowed("https://evil.example", allowed), "DNS-rebinding defence"
