"""Change hints over ``subscriptions/listen``.

Legacy MCP opened a standalone SSE stream and sent
``notifications/resources/updated`` down it. 2026-07-28 removed both the GET
endpoint and ``resources/subscribe``: a client now sends *one*
``subscriptions/listen`` request whose **response stream** carries the
notifications, and the server must acknowledge before it sends anything.

Two invariants are enforced here rather than assumed, because both fail
silently in production and are trivially testable in a unit:

1. The acknowledgement is the **first** frame and carries
   ``io.modelcontextprotocol/subscriptionId`` in ``_meta``. Anything before
   it is a protocol error, not an early notification.
2. The stream carries **only requested types**. A server that volunteers
   extra notifications is teaching consumers to depend on delivery, which
   is precisely the coupling the refetch rule exists to prevent.

And the standing rule from the portable core: a hint is never authoritative.
Its only legal effect is to make a consumer refetch — see
:mod:`~mcp_heartbeat_current.convergence`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

from mcp_heartbeat.ports import ChangeHint

from .contract import (
    ACKNOWLEDGED_NOTIFICATION,
    HEARTBEAT_HINT_META_KEY,
    LISTEN_METHOD,
    LISTEN_STREAM_METHODS,
    PROTOCOL_REVISION,
    RESOURCE_UPDATED_NOTIFICATION,
    SUBSCRIPTION_ID_META_KEY,
)
from .errors import SubscriptionProtocolError


@dataclass(frozen=True)
class SubscriptionFilter:
    """What a listener is asking to hear about.

    ``resource_subscriptions`` holds lease URIs. A heartbeat consumer asks
    for exactly the participants it tracks and nothing else, so an operator
    reading a packet capture can see the blast radius of one consumer.
    """

    tools_list_changed: bool = False
    prompts_list_changed: bool = False
    resources_list_changed: bool = False
    resource_subscriptions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "toolsListChanged": self.tools_list_changed,
            "promptsListChanged": self.prompts_list_changed,
            "resourcesListChanged": self.resources_list_changed,
            "resourceSubscriptions": list(self.resource_subscriptions),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubscriptionFilter":
        return cls(
            tools_list_changed=bool(raw.get("toolsListChanged", False)),
            prompts_list_changed=bool(raw.get("promptsListChanged", False)),
            resources_list_changed=bool(raw.get("resourcesListChanged", False)),
            resource_subscriptions=tuple(raw.get("resourceSubscriptions", ()) or ()),
        )

    def honoured_methods(self) -> frozenset[str]:
        """Notification methods this filter legitimises."""
        methods: set[str] = set()
        if self.tools_list_changed:
            methods.add("notifications/tools/list_changed")
        if self.prompts_list_changed:
            methods.add("notifications/prompts/list_changed")
        if self.resources_list_changed:
            methods.add("notifications/resources/list_changed")
        if self.resource_subscriptions:
            methods.add(RESOURCE_UPDATED_NOTIFICATION)
        return frozenset(methods)

    @classmethod
    def for_participants(
        cls, uris: Iterable[str], *, include_resource_list: bool = False
    ) -> "SubscriptionFilter":
        """The filter a heartbeat consumer wants: lease URIs, nothing else."""
        return cls(
            resources_list_changed=include_resource_list,
            resource_subscriptions=tuple(uris),
        )


def build_listen_request(
    subscription_filter: SubscriptionFilter,
    *,
    request_id: str | int,
    protocol_version: str = PROTOCOL_REVISION,
    client_info: Mapping[str, Any] | None = None,
    client_capabilities: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """One request that opens one subscription stream."""
    from .metadata import build_request

    return build_request(
        LISTEN_METHOD,
        {"notifications": subscription_filter.to_dict()},
        request_id=request_id,
        protocol_version=protocol_version,
        client_info=client_info or {},
        client_capabilities=client_capabilities or {},
    )


def build_acknowledgement(
    subscription_id: str | int, honoured: SubscriptionFilter
) -> dict[str, Any]:
    """The first frame a server must send on the stream.

    ``honoured`` may be narrower than what was asked for — a server is
    allowed to decline part of a filter, and saying so up front is what lets
    a consumer fall back to timed refetch for the declined part instead of
    waiting forever.
    """
    return {
        "jsonrpc": "2.0",
        "method": ACKNOWLEDGED_NOTIFICATION,
        "params": {
            "notifications": honoured.to_dict(),
            "_meta": {SUBSCRIPTION_ID_META_KEY: subscription_id},
        },
    }


def build_resource_updated(
    uri: str, hint: ChangeHint | None = None, *, subscription_id: str | int | None = None
) -> dict[str, Any]:
    """A change notification for ``uri``.

    The modern ``ResourceUpdated`` event carries only a URI. Revision and
    digest ride in ``_meta`` under the prefixed heartbeat key — which is
    what ``_meta`` is *for*, and which keeps the notification valid for a
    peer that has never heard of heartbeat.
    """
    meta: dict[str, Any] = {}
    if subscription_id is not None:
        meta[SUBSCRIPTION_ID_META_KEY] = subscription_id
    if hint is not None:
        meta[HEARTBEAT_HINT_META_KEY] = {
            "revision": hint.revision,
            "digest": hint.digest,
        }
    params: dict[str, Any] = {"uri": uri}
    if meta:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "method": RESOURCE_UPDATED_NOTIFICATION, "params": params}


def parse_hint(notification: Mapping[str, Any]) -> ChangeHint | None:
    """Read a ``ChangeHint`` out of a resource-updated notification.

    Returns ``None`` when the notification carries no heartbeat ``_meta``
    — a bare URI is still a perfectly good "something changed, go look",
    and degrading to an unqualified refetch is strictly safer than
    inventing a revision.
    """
    if notification.get("method") != RESOURCE_UPDATED_NOTIFICATION:
        return None
    params = notification.get("params")
    if not isinstance(params, Mapping):
        return None
    uri = params.get("uri")
    if not isinstance(uri, str):
        return None
    meta = params.get("_meta")
    payload = meta.get(HEARTBEAT_HINT_META_KEY) if isinstance(meta, Mapping) else None
    if not isinstance(payload, Mapping):
        return None
    try:
        return ChangeHint.from_dict(
            {
                "address": uri,
                "revision": payload.get("revision"),
                "digest": payload.get("digest"),
            }
        )
    except Exception:
        # A malformed hint is a hint we ignore, not an error we propagate:
        # the consumer's refetch deadline still fires. Trusting a hint enough
        # to fail on it would make delivery load-bearing.
        return None


def changed_uri(notification: Mapping[str, Any]) -> str | None:
    """The lease URI a notification refers to, hint metadata or not."""
    params = notification.get("params")
    if not isinstance(params, Mapping):
        return None
    uri = params.get("uri")
    return uri if isinstance(uri, str) else None


@dataclass
class SubscriptionStream:
    """Client-side view of one ``subscriptions/listen`` response stream.

    Stateful by nature — the ack-before-anything rule is an ordering
    property, and ordering cannot be checked by a pure function over a
    single frame.
    """

    subscription_filter: SubscriptionFilter
    subscription_id: str | int | None = None
    honoured: SubscriptionFilter | None = None
    acknowledged: bool = False
    #: Notifications accepted, in arrival order. Bounded by the caller's
    #: own consumption; this class never buffers unboundedly on its own.
    received: list[Mapping[str, Any]] = field(default_factory=list)

    def accept(self, frame: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate one inbound frame against the stream's contract."""
        method = frame.get("method")

        if method == ACKNOWLEDGED_NOTIFICATION:
            if self.acknowledged:
                raise SubscriptionProtocolError("subscription acknowledged twice")
            params = frame.get("params")
            meta = params.get("_meta") if isinstance(params, Mapping) else None
            subscription_id = meta.get(SUBSCRIPTION_ID_META_KEY) if isinstance(meta, Mapping) else None
            if subscription_id is None:
                raise SubscriptionProtocolError(
                    f"acknowledgement is missing _meta[{SUBSCRIPTION_ID_META_KEY!r}]"
                )
            self.acknowledged = True
            self.subscription_id = subscription_id
            honoured = params.get("notifications") if isinstance(params, Mapping) else None
            self.honoured = (
                SubscriptionFilter.from_dict(honoured)
                if isinstance(honoured, Mapping)
                else self.subscription_filter
            )
            return frame

        if not self.acknowledged:
            raise SubscriptionProtocolError(
                f"{method!r} arrived before {ACKNOWLEDGED_NOTIFICATION}"
            )
        if method not in LISTEN_STREAM_METHODS:
            raise SubscriptionProtocolError(f"{method!r} is not a subscription-stream notification")
        honoured = self.honoured or self.subscription_filter
        if method not in honoured.honoured_methods():
            raise SubscriptionProtocolError(f"{method!r} was never requested")

        self.received.append(frame)
        return frame

    def hints(self) -> Iterator[tuple[str, ChangeHint | None]]:
        """``(uri, hint_or_None)`` for every resource-change frame accepted."""
        for frame in self.received:
            if frame.get("method") != RESOURCE_UPDATED_NOTIFICATION:
                continue
            uri = changed_uri(frame)
            if uri is not None:
                yield uri, parse_hint(frame)


__all__ = [
    "SubscriptionFilter",
    "SubscriptionStream",
    "build_acknowledgement",
    "build_listen_request",
    "build_resource_updated",
    "changed_uri",
    "parse_hint",
]
