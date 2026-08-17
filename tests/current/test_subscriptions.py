"""``subscriptions/listen`` — MCP-HB-03-S2-T01."""
from __future__ import annotations

import pytest

from mcp_heartbeat.ports import ChangeHint

from mcp_heartbeat_current import contract
from mcp_heartbeat_current.errors import SubscriptionProtocolError
from mcp_heartbeat_current.metadata import classify_request
from mcp_heartbeat_current.subscriptions import (
    SubscriptionFilter,
    SubscriptionStream,
    build_acknowledgement,
    build_listen_request,
    build_resource_updated,
    changed_uri,
    parse_hint,
)

URI = "heartbeat://svc/api-7"
DIGEST = "sha256:" + "ab" * 32


def _filter() -> SubscriptionFilter:
    return SubscriptionFilter.for_participants([URI])


def _open_stream() -> SubscriptionStream:
    stream = SubscriptionStream(subscription_filter=_filter())
    stream.accept(build_acknowledgement("listen-1", _filter()))
    return stream


# ── one request opens one stream ──────────────────────────────────────


def test_change_delivery_is_a_request_not_a_standalone_stream() -> None:
    """Acceptance: no standalone notification transport is required."""
    body, headers = build_listen_request(_filter(), request_id="listen-1")
    assert body["method"] == "subscriptions/listen"
    route = classify_request(body, headers)
    assert route.method == "subscriptions/listen"
    assert body["params"]["notifications"]["resourceSubscriptions"] == [URI]


def test_the_filter_asks_only_for_the_participants_being_tracked() -> None:
    honoured = _filter().honoured_methods()
    assert honoured == {contract.RESOURCE_UPDATED_NOTIFICATION}
    assert "notifications/tools/list_changed" not in honoured


# ── the acknowledgement contract ──────────────────────────────────────


def test_the_acknowledgement_carries_the_subscription_id() -> None:
    ack = build_acknowledgement("listen-1", _filter())
    assert ack["method"] == contract.ACKNOWLEDGED_NOTIFICATION
    assert ack["params"]["_meta"][contract.SUBSCRIPTION_ID_META_KEY] == "listen-1"


def test_nothing_may_arrive_before_the_acknowledgement() -> None:
    stream = SubscriptionStream(subscription_filter=_filter())
    with pytest.raises(SubscriptionProtocolError) as excinfo:
        stream.accept(build_resource_updated(URI))
    assert contract.ACKNOWLEDGED_NOTIFICATION in str(excinfo.value)


def test_an_acknowledgement_without_a_subscription_id_is_a_protocol_error() -> None:
    stream = SubscriptionStream(subscription_filter=_filter())
    ack = build_acknowledgement("listen-1", _filter())
    del ack["params"]["_meta"]
    with pytest.raises(SubscriptionProtocolError):
        stream.accept(ack)


def test_a_second_acknowledgement_is_refused() -> None:
    stream = _open_stream()
    with pytest.raises(SubscriptionProtocolError):
        stream.accept(build_acknowledgement("listen-1", _filter()))


def test_a_server_may_honour_less_than_was_asked_for() -> None:
    """Saying so up front lets the consumer fall back to timed refetch."""
    stream = SubscriptionStream(
        subscription_filter=SubscriptionFilter(
            resources_list_changed=True, resource_subscriptions=(URI,)
        )
    )
    stream.accept(build_acknowledgement("listen-1", _filter()))
    assert stream.honoured is not None
    assert not stream.honoured.resources_list_changed
    with pytest.raises(SubscriptionProtocolError):
        stream.accept({"jsonrpc": "2.0", "method": "notifications/resources/list_changed"})


def test_unrequested_notification_types_are_refused() -> None:
    """The server MUST NOT send a type the client did not request."""
    stream = _open_stream()
    with pytest.raises(SubscriptionProtocolError):
        stream.accept({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


def test_a_non_stream_notification_is_refused() -> None:
    stream = _open_stream()
    with pytest.raises(SubscriptionProtocolError):
        stream.accept({"jsonrpc": "2.0", "method": "notifications/message"})


# ── hints ─────────────────────────────────────────────────────────────


def test_a_hint_carries_revision_and_digest_under_a_prefixed_meta_key() -> None:
    hint = ChangeHint(address=URI, revision="epoch-a:3", digest=DIGEST)
    notification = build_resource_updated(URI, hint, subscription_id="listen-1")
    meta = notification["params"]["_meta"]
    assert contract.HEARTBEAT_HINT_META_KEY in meta
    assert meta[contract.HEARTBEAT_HINT_META_KEY]["revision"] == "epoch-a:3"
    assert parse_hint(notification) == hint


def test_a_bare_notification_is_still_a_usable_go_and_look() -> None:
    """Degrading to an unqualified refetch beats inventing a revision."""
    notification = build_resource_updated(URI)
    assert parse_hint(notification) is None
    assert changed_uri(notification) == URI


def test_a_malformed_hint_is_ignored_rather_than_raised() -> None:
    """Failing on a hint would make delivery load-bearing."""
    notification = build_resource_updated(URI)
    notification["params"]["_meta"] = {
        contract.HEARTBEAT_HINT_META_KEY: {"revision": "epoch-a:3", "digest": "not-a-digest"}
    }
    assert parse_hint(notification) is None
    assert changed_uri(notification) == URI


def test_a_notification_for_another_method_yields_no_hint() -> None:
    assert parse_hint({"method": "notifications/tools/list_changed", "params": {}}) is None


def test_the_stream_reports_every_accepted_change_with_its_hint() -> None:
    stream = _open_stream()
    hint = ChangeHint(address=URI, revision="epoch-a:4", digest=DIGEST)
    stream.accept(build_resource_updated(URI, hint))
    stream.accept(build_resource_updated(URI))
    assert list(stream.hints()) == [(URI, hint), (URI, None)]


def test_a_hint_is_never_admissible_as_a_heartbeat() -> None:
    """The core's own guarantee, restated at the adapter seam."""
    from mcp_heartbeat.lineage import LineageState, admit
    from mcp_heartbeat.clock import FakeClock

    hint = ChangeHint(address=URI, revision="epoch-a:4", digest=DIGEST)
    clock = FakeClock()
    admission = admit(LineageState(participant_id="svc/api-7"), hint.to_dict(), clock.now())
    assert not admission.accepted
