"""The pinned contract: values, and the axes they must not collapse into."""
from __future__ import annotations

import pytest

from mcp_heartbeat.model import EXTENSION_VERSION

from mcp_heartbeat_current import contract


def test_the_adapter_speaks_exactly_one_modern_revision() -> None:
    assert contract.PROTOCOL_REVISION == "2026-07-28"
    assert contract.MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)
    assert contract.is_modern("2026-07-28")
    assert not contract.is_modern("2025-06-18")


def test_handshake_revisions_are_recognised_but_not_spoken() -> None:
    """Naming them is how they get refused; speaking them is the defect."""
    for version in contract.HANDSHAKE_PROTOCOL_VERSIONS:
        assert contract.is_handshake(version)
        assert not contract.is_modern(version)
    assert set(contract.HANDSHAKE_PROTOCOL_VERSIONS).isdisjoint(contract.MODERN_PROTOCOL_VERSIONS)


def test_extension_version_and_protocol_revision_are_independent_axes() -> None:
    """The HB-00 finding this PRD exists to not repeat.

    ``extension_version`` versions the lease contract; ``PROTOCOL_REVISION``
    versions the MCP wire. If either were derived from the other, bumping
    one would silently bump the other.
    """
    assert contract.HEARTBEAT_EXTENSION_VERSION == EXTENSION_VERSION
    assert contract.HEARTBEAT_EXTENSION_VERSION != contract.PROTOCOL_REVISION
    assert contract.HEARTBEAT_EXTENSION_VERSION not in contract.MODERN_PROTOCOL_VERSIONS
    assert contract.PROTOCOL_REVISION not in {contract.HEARTBEAT_EXTENSION_VERSION}


def test_the_extension_identifier_is_prefixed_and_vendor_owned() -> None:
    """D-N5: implementation-owned until a body assigns a neutral id."""
    assert contract.HEARTBEAT_EXTENSION_ID == "com.dougfirlabs/heartbeat"
    assert contract.is_valid_extension_identifier(contract.HEARTBEAT_EXTENSION_ID)
    assert not contract.HEARTBEAT_EXTENSION_ID.startswith("io.modelcontextprotocol/")


def test_the_extension_identifier_is_declared_exactly_once() -> None:
    """One constant, so replacing it is a one-line change."""
    from pathlib import Path

    package = Path(contract.__file__).resolve().parent
    literal = '"com.dougfirlabs/heartbeat"'
    hits = {
        path.name: path.read_text(encoding="utf-8").count(literal)
        for path in package.glob("*.py")
    }
    assert hits["contract.py"] == 1
    assert sum(count for name, count in hits.items() if name != "contract.py") == 0


@pytest.mark.parametrize(
    "identifier,valid",
    [
        ("com.dougfirlabs/heartbeat", True),
        ("io.modelcontextprotocol/tasks", True),
        ("a.b.c/d.e-f", True),
        ("experimental.presenceLease", False),  # the HB-00 defect: no prefix
        ("noslash", False),
        ("/leading", False),
        ("trailing/", False),
        ("", False),
        (None, False),
    ],
)
def test_extension_identifier_grammar(identifier: object, valid: bool) -> None:
    assert contract.is_valid_extension_identifier(identifier) is valid


def test_error_codes_match_the_modern_revision() -> None:
    assert contract.HEADER_MISMATCH == -32020
    assert contract.MISSING_REQUIRED_CLIENT_CAPABILITY == -32021
    assert contract.UNSUPPORTED_PROTOCOL_VERSION == -32022
    assert contract.ERROR_CODE_HTTP_STATUS[contract.HEADER_MISMATCH] == 400


def test_meta_keys_are_the_standard_prefixed_names() -> None:
    for key in (
        contract.PROTOCOL_VERSION_META_KEY,
        contract.CLIENT_INFO_META_KEY,
        contract.CLIENT_CAPABILITIES_META_KEY,
        contract.SERVER_INFO_META_KEY,
        contract.SUBSCRIPTION_ID_META_KEY,
    ):
        assert key.startswith("io.modelcontextprotocol/")
        assert contract.is_valid_extension_identifier(key)


def test_the_heartbeat_hint_key_is_namespaced_to_this_implementation() -> None:
    """``_meta`` keys need a prefix, and ours may not be the standards one."""
    assert contract.is_valid_extension_identifier(contract.HEARTBEAT_HINT_META_KEY)
    assert not contract.HEARTBEAT_HINT_META_KEY.startswith("io.modelcontextprotocol/")


def test_the_sdk_pin_is_exact() -> None:
    """A range would let a minor release move a wire constant silently."""
    assert contract.SDK_PIN.distribution == "mcp"
    assert contract.SDK_PIN.version == "2.0.0"
    assert contract.SDK_PIN.types_version == "2.0.0"
    assert contract.SDK_PIN.implements_revision == contract.PROTOCOL_REVISION


def test_every_hb00_forbidden_primitive_is_carried_forward() -> None:
    """The table travels with the package, not only with the evidence pack."""
    named = {p.primitive for p in contract.FORBIDDEN_PRIMITIVES}
    assert named == {
        "initialize",
        "notifications/initialized",
        "Mcp-Session-Id",
        "GET <mcp endpoint> -> text/event-stream",
        "resources/subscribe",
        "Last-Event-ID",
        "server-initiated JSON-RPC requests on an SSE stream",
        "experimental.presenceLease",
    }


def test_highest_mutual_version_picks_the_newest_shared_revision() -> None:
    assert contract.highest_mutual_version(["2026-07-28"]) == "2026-07-28"
    assert contract.highest_mutual_version(["2025-06-18"]) is None
    assert contract.highest_mutual_version([]) is None
    assert contract.highest_mutual_version(None) is None
    assert contract.highest_mutual_version("2026-07-28") is None  # a string is not a list


def test_listen_stream_methods_exclude_lifecycle_traffic() -> None:
    assert contract.RESOURCE_UPDATED_NOTIFICATION in contract.LISTEN_STREAM_METHODS
    assert contract.ACKNOWLEDGED_NOTIFICATION not in contract.LISTEN_STREAM_METHODS
