"""Advertise only what is actually served. Defect D-02, structurally.

The archived legacy server advertised ``resources.subscribe = true`` from a
hand-written literal while its method registry contained no
``resources/subscribe``. A conformant client that believed the advertisement
got ``METHOD_NOT_FOUND``. Nothing detected the disagreement because the
advertisement and the registry were two independent facts that happened to
disagree.

The repair is to stop having two facts. :func:`advertise` *derives* the
capability object from the registry, so an advertisement without a handler
cannot be written; :func:`agreement_violations` audits an advertisement that
came from somewhere else — a peer's, or a hand-built one in a test — and
reports both directions of mismatch.

Stdlib plus the portable core.
"""
from __future__ import annotations

from typing import Any, Collection, Mapping

from mcp_heartbeat.model import EXTENSION_VERSION

from .era import (
    HEARTBEAT_CAPABILITY,
    HEARTBEAT_NAMESPACE,
    MODERN_EXTENSION_ID,
)

#: Reading the authoritative heartbeat document. Advertising the heartbeat
#: extension without this handler is meaningless: a change hint is advisory,
#: so a consumer that cannot refetch cannot participate at all.
AUTHORITATIVE_READ_METHOD = "resources/read"

#: The registry entry ``resources.subscribe`` claims.
SUBSCRIBE_METHOD = "resources/subscribe"
UNSUBSCRIBE_METHOD = "resources/unsubscribe"

#: The registry entry ``resources.listChanged`` claims.
LIST_CHANGED_METHOD = "notifications/resources/list_changed"


def advertise(implemented: Collection[str]) -> dict[str, Any]:
    """Build the ``initialize`` capability object from the method registry.

    Every flag is a question asked of ``implemented``, never a literal. The
    heartbeat extension is advertised only when the authoritative read exists,
    for the reason in this module's docstring.
    """
    served = set(implemented)
    capabilities: dict[str, Any] = {
        "resources": {
            "subscribe": SUBSCRIBE_METHOD in served,
            "listChanged": LIST_CHANGED_METHOD in served,
        }
    }
    if AUTHORITATIVE_READ_METHOD in served:
        capabilities[HEARTBEAT_NAMESPACE] = {
            HEARTBEAT_CAPABILITY: {"extension_version": EXTENSION_VERSION}
        }
    return capabilities


def agreement_violations(
    advertised: Mapping[str, Any], implemented: Collection[str]
) -> list[str]:
    """Report every place an advertisement and a registry disagree.

    Both directions matter. Advertising an unserved method is D-02 itself;
    serving an unadvertised one is the mirror defect — a client that trusts
    the advertisement will never call it, so the handler is dead code that
    looks alive.
    """
    served = set(implemented)
    out: list[str] = []

    resources = advertised.get("resources") or {}
    if not isinstance(resources, Mapping):
        return ["capabilities.resources must be an object"]

    for flag, method in (
        ("subscribe", SUBSCRIBE_METHOD),
        ("listChanged", LIST_CHANGED_METHOD),
    ):
        claimed = bool(resources.get(flag))
        present = method in served
        if claimed and not present:
            out.append(f"advertises resources.{flag} but serves no {method} handler")
        elif present and not claimed:
            out.append(f"serves {method} but does not advertise resources.{flag}")

    experimental = advertised.get(HEARTBEAT_NAMESPACE) or {}
    heartbeat_claimed = (
        isinstance(experimental, Mapping) and HEARTBEAT_CAPABILITY in experimental
    )
    if heartbeat_claimed and AUTHORITATIVE_READ_METHOD not in served:
        out.append(
            f"advertises {HEARTBEAT_NAMESPACE}.{HEARTBEAT_CAPABILITY} but serves no "
            f"{AUTHORITATIVE_READ_METHOD}; a hint alone cannot carry a lease"
        )

    extensions = advertised.get("extensions") or {}
    if isinstance(extensions, Mapping) and MODERN_EXTENSION_ID in extensions:
        out.append(
            f"advertises the modern identifier {MODERN_EXTENSION_ID} on a legacy "
            "session; era boundaries must be explicit"
        )

    return out


__all__ = [
    "AUTHORITATIVE_READ_METHOD",
    "LIST_CHANGED_METHOD",
    "SUBSCRIBE_METHOD",
    "UNSUBSCRIBE_METHOD",
    "advertise",
    "agreement_violations",
]
