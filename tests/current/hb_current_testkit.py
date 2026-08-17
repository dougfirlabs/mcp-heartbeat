"""Shared test doubles for the current-era adapter.

Deliberately *not* in ``conftest.py``. A validation command may run
this directory alongside another test tree from a parent repository
root, which makes that repository's own ``tests/conftest.py`` the first
``conftest`` on ``sys.path`` — so ``from conftest import ...`` silently
resolves to the wrong module and collection fails. A uniquely named module
cannot collide with anything in either tree.

Everything here is deterministic and offline: nothing sleeps, nothing opens
a socket, and every fault is injected by setting a counter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from mcp_heartbeat.issuer import HeartbeatIssuer
from mcp_heartbeat.model import Heartbeat

from mcp_heartbeat_current.convergence import FetchResult
from mcp_heartbeat_current.identity import Principal

PARTICIPANT = "svc/api-7"

#: The lease URI for :data:`PARTICIPANT` under the default template, with
#: the id percent-encoded into one path segment.
ADDRESS = "heartbeat://participants/svc%2Fapi-7"


class Unreachable(RuntimeError):
    """The transport failed. Says nothing about the participant."""


@dataclass
class Publisher:
    """An in-memory producer: one participant, one epoch, a lease stream."""

    issuer: HeartbeatIssuer
    documents: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    latest: Heartbeat | None = None

    def publish(self) -> Heartbeat:
        """Mint and publish the next lease in this epoch."""
        heartbeat = self.issuer.issue()
        self.latest = heartbeat
        self.documents[heartbeat.participant_id] = heartbeat.to_dict()
        return heartbeat

    def restart(self, epoch_id: str, *, lease_seconds: float | None = None) -> None:
        """Model a process restart: new epoch, sequence back to zero."""
        self.issuer = HeartbeatIssuer(
            participant_id=self.issuer.participant_id,
            epoch_id=epoch_id,
            clock=self.issuer.clock,
            lease_seconds=lease_seconds
            if lease_seconds is not None
            else self.issuer.lease_seconds,
        )


@dataclass
class ScriptedSource:
    """An authoritative source that can be told to fail.

    ``failures`` is a countdown rather than a flag so a test can model
    "unreachable for exactly two attempts, then recovered" without having to
    reach into the fixture mid-test.
    """

    documents: dict[str, Mapping[str, Any]]
    principal: Principal | None = None
    failures: int = 0
    fetches: int = 0

    def fetch(self, participant_id: str) -> FetchResult:
        self.fetches += 1
        if self.failures > 0:
            self.failures -= 1
            raise Unreachable(f"cannot reach {participant_id}")
        document = self.documents.get(participant_id)
        if document is None:
            raise Unreachable(f"no lease published for {participant_id}")
        return FetchResult(document=document, principal=self.principal)


__all__ = ["ADDRESS", "PARTICIPANT", "Publisher", "ScriptedSource", "Unreachable"]
