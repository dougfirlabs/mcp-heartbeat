"""Fixtures for the current-era adapter.

Everything here is deterministic and offline: a ``FakeClock``, an in-memory
publisher, and a source that can be told to fail. Nothing sleeps and nothing
opens a socket, so the whole delivery-fault matrix runs in milliseconds and
a failure is reproducible from the test name alone.

The parent ``tests/conftest.py`` already put ``src`` on ``sys.path``; this
module adds only fixtures.
"""
from __future__ import annotations

import pytest

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.issuer import HeartbeatIssuer

from mcp_heartbeat_current.convergence import HeartbeatConsumer
from mcp_heartbeat_current.discovery import (
    HeartbeatCapability,
    build_discover_result,
    negotiate,
)
from mcp_heartbeat_current.identity import (
    Principal,
    RequestIdentityBinder,
    StaticPermittedParticipants,
)

from hb_current_testkit import PARTICIPANT, Publisher, ScriptedSource


@pytest.fixture()
def anyio_backend() -> str:
    """Pin the async backend for the SDK conformance tests.

    Only ``test_sdk_conformance.py`` uses it, and only in the isolated
    environment where the official SDK is installed. asyncio rather than
    trio because that is what the SDK's own in-process dispatcher runs on.
    """
    return "asyncio"


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def publisher(clock: FakeClock) -> Publisher:
    return Publisher(
        issuer=HeartbeatIssuer(
            participant_id=PARTICIPANT, epoch_id="epoch-a", clock=clock, lease_seconds=30.0
        )
    )


@pytest.fixture()
def source(publisher: Publisher) -> ScriptedSource:
    return ScriptedSource(documents=publisher.documents)


@pytest.fixture()
def consumer(source: ScriptedSource, clock: FakeClock) -> HeartbeatConsumer:
    """A consumer with no identity binder — pure lineage behaviour."""
    return HeartbeatConsumer(PARTICIPANT, source, clock)


@pytest.fixture()
def principal() -> Principal:
    return Principal(client_id="gw-1", issuer="https://idp.example", subject="svc-api-7")


@pytest.fixture()
def policy(principal: Principal) -> StaticPermittedParticipants:
    """The injected principal → permitted-participant map (D-N3)."""
    return StaticPermittedParticipants({principal.compact(): {PARTICIPANT}})


@pytest.fixture()
def binder_factory(policy: StaticPermittedParticipants):
    """Builds a fresh per-request binder, as a real transport would."""

    def _factory(request_principal: Principal | None) -> RequestIdentityBinder:
        return RequestIdentityBinder(policy, request_principal)

    return _factory


@pytest.fixture()
def negotiation():
    """A negotiation against a server that advertises heartbeat 0.1."""
    return negotiate(
        build_discover_result(
            server_info={"name": "lab", "version": "0.1.0"},
            capability=HeartbeatCapability(),
        )
    )
