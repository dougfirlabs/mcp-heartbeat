"""The legacy lifecycle, completed in both directions. Defect D-03.

The archived lab asserted in a comment that ``notifications/initialized`` "is
the one the handshake sends". The client never sent it and the server had no
handler, so the handshake was half-finished on both sides. It looked healthy
only because an unknown *notification* is answered with silence — the failure
mode of the legacy contract is to say nothing.

Here the notification is sent by :class:`LegacyClientSession`, handled by
:class:`LegacyServerSession`, and load-bearing: a server session refuses every
ordinary request until it arrives. A handshake that is not completed now fails
loudly instead of degrading quietly.

The session is also where D-04's ledger lives. Every rejected or ambiguous
version request is appended to :attr:`LegacyServerSession.disagreements` and
surfaced by :meth:`LegacyServerSession.stats`, so a downgrade-confusion test
has something to assert against.

Transport-neutral: methods and params in, results out. No sockets, no HTTP,
no SDK.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Collection, Mapping

from .capabilities import AUTHORITATIVE_READ_METHOD, advertise
from .era import (
    LEGACY_ERA,
    EraReport,
    HeartbeatNegotiation,
    NegotiationCode,
    NegotiationOutcome,
    ProtocolNegotiation,
    UNNEGOTIATED,
    assert_legacy_era,
    negotiate_heartbeat,
    negotiate_protocol,
)
from .errors import JsonRpcCode, LegacyProtocolError, LegacyReason

INITIALIZE = "initialize"
INITIALIZED_NOTIFICATION = "notifications/initialized"


def _not_offered(detail: str) -> HeartbeatNegotiation:
    """This side declined to negotiate the extension. Not an error."""
    return HeartbeatNegotiation(
        NegotiationOutcome.UNSUPPORTED, NegotiationCode.CAPABILITY_ABSENT, None, detail
    )


class SessionState(str, Enum):
    """Where a session is in the legacy lifecycle.

    ``INITIALIZING`` is the state the archived implementation had no name for:
    ``initialize`` answered, ``initialized`` not yet received. Naming it is
    what makes D-03 expressible.
    """

    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class Disagreement:
    """One recorded protocol-version disagreement (D-04)."""

    requested: object
    code: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested if isinstance(self.requested, str) else repr(self.requested),
            "code": self.code,
            "detail": self.detail,
        }


class LegacyServerSession:
    """Server half of one legacy MCP connection.

    Owns lifecycle state, negotiation, and the capability advertisement. It
    does not own resources: ``handle`` dispatches non-lifecycle methods to the
    registry it was given, so the heartbeat resource surface stays in
    :mod:`.resources` and ordinary MCP methods stay wherever the deployment
    put them.
    """

    def __init__(
        self,
        *,
        server_name: str,
        methods: Mapping[str, Any] | None = None,
        implemented: Collection[str] | None = None,
        era: str = LEGACY_ERA,
    ) -> None:
        self.server_name = server_name
        self.era = assert_legacy_era(era)
        self._methods: dict[str, Any] = dict(methods or {})
        # A registry of callables is the usual case; `implemented` exists for
        # deployments that route elsewhere and only need the names known.
        self._implemented = set(implemented or ()) | set(self._methods)
        self.state = SessionState.CREATED
        self.disagreements: list[Disagreement] = []
        self._protocol: ProtocolNegotiation | None = None
        self._heartbeat: HeartbeatNegotiation | None = None

    # ── reporting ────────────────────────────────────────────────

    @property
    def era_report(self) -> EraReport:
        """The two version axes, separately. Never one conflated string."""
        if self._protocol is None or not self._protocol.accepted:
            return UNNEGOTIATED
        heartbeat = self._heartbeat
        return EraReport(
            mcp_protocol_era=self._protocol.negotiated,
            extension_version=heartbeat.extension_version if heartbeat else None,
            heartbeat_supported=bool(heartbeat and heartbeat.supported),
        )

    @property
    def heartbeat_ready(self) -> bool:
        """Handshake complete *and* the heartbeat extension negotiated."""
        return self.state is SessionState.READY and self.era_report.heartbeat_supported

    def capabilities(self) -> dict[str, Any]:
        """What this server advertises — derived, never hand-written (D-02)."""
        return advertise(self._implemented)

    def stats(self) -> dict[str, Any]:
        """Operational ledger. Names the protocol version, unlike the baseline."""
        return {
            "state": self.state.value,
            "protocol_version": self.era_report.mcp_protocol_era,
            "extension_version": self.era_report.extension_version,
            "heartbeat_supported": self.era_report.heartbeat_supported,
            "protocol_version_disagreements": [d.to_dict() for d in self.disagreements],
        }

    # ── dispatch ─────────────────────────────────────────────────

    def handle(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Handle one request or notification. Raises to fail closed."""
        params = params or {}

        if method == INITIALIZE:
            return self._handle_initialize(params)
        if method == INITIALIZED_NOTIFICATION:
            return self._handle_initialized()

        if self.state is not SessionState.READY:
            raise LegacyProtocolError(
                LegacyReason.SESSION_NOT_INITIALIZED,
                f"{method!r} arrived before the handshake completed "
                f"(state={self.state.value}); the legacy lifecycle requires "
                f"{INITIALIZE} then {INITIALIZED_NOTIFICATION}",
            )
        if method not in self._implemented:
            raise LegacyProtocolError(
                LegacyReason.METHOD_NOT_IMPLEMENTED,
                f"no handler for {method!r}",
                code=JsonRpcCode.METHOD_NOT_FOUND,
            )
        handler = self._methods.get(method)
        if handler is None:
            raise LegacyProtocolError(
                LegacyReason.METHOD_NOT_IMPLEMENTED,
                f"{method!r} is declared implemented but routes to nothing",
                code=JsonRpcCode.METHOD_NOT_FOUND,
            )
        return handler(params)

    def _handle_initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if self.state is not SessionState.CREATED:
            raise LegacyProtocolError(
                LegacyReason.LIFECYCLE_OUT_OF_ORDER,
                f"{INITIALIZE} is legal once per session (state={self.state.value})",
            )

        protocol = negotiate_protocol(params.get("protocolVersion", None))
        if protocol.disagreement:
            self.disagreements.append(
                Disagreement(protocol.requested, protocol.code.value, protocol.detail)
            )
        if not protocol.accepted:
            # Fail closed. There is no negotiated value to echo, which is the
            # whole point of D-04's repair.
            self.state = SessionState.FAILED
            self._protocol = protocol
            raise LegacyProtocolError(
                LegacyReason.NEGOTIATION_FAILED,
                protocol.detail,
                code=JsonRpcCode.INVALID_PARAMS,
            )

        # Negotiation is bilateral: the extension is in effect only if the
        # peer offered it *and* this side can serve it. Reading the peer's
        # advertisement alone is how one end ends up believing in a feature
        # the other never had.
        heartbeat = negotiate_heartbeat(params.get("capabilities"))
        if heartbeat.supported and AUTHORITATIVE_READ_METHOD not in self._implemented:
            heartbeat = _not_offered(
                f"this server serves no {AUTHORITATIVE_READ_METHOD} and cannot "
                "carry a heartbeat lease"
            )
        self._protocol = protocol
        self._heartbeat = heartbeat
        self.state = SessionState.INITIALIZING

        result: dict[str, Any] = {
            "protocolVersion": protocol.negotiated,
            "capabilities": self.capabilities(),
            "serverInfo": {"name": self.server_name, "version": "0.1.0"},
        }
        # An unsupported or ambiguous *heartbeat* advertisement is not fatal:
        # ordinary MCP continues and the peer is told it is heartbeat-
        # unsupported. Only the protocol version can end a session.
        if heartbeat.outcome is not NegotiationOutcome.ACCEPTED:
            result["instructions"] = f"heartbeat extension not negotiated: {heartbeat.detail}"
        return result

    def _handle_initialized(self) -> None:
        if self.state is not SessionState.INITIALIZING:
            raise LegacyProtocolError(
                LegacyReason.LIFECYCLE_OUT_OF_ORDER,
                f"{INITIALIZED_NOTIFICATION} is legal exactly once, after "
                f"{INITIALIZE} (state={self.state.value})",
            )
        self.state = SessionState.READY
        return None


class LegacyClientSession:
    """Client half of one legacy MCP connection.

    Sends ``notifications/initialized`` — the half of D-03 the archived client
    omitted — and refuses a server that answers with a revision it never
    offered, which is the client-side guard against a silent downgrade.
    """

    def __init__(
        self,
        *,
        client_name: str,
        requested_protocol_version: str = LEGACY_ERA,
        request_heartbeat: bool = True,
        auth_context: Any = None,
    ) -> None:
        self.client_name = client_name
        self.requested_protocol_version = assert_legacy_era(requested_protocol_version)
        self.request_heartbeat = request_heartbeat
        #: The session's authenticated principal, if the transport supplied
        #: one. Held here because the *session* is the strongest identity
        #: context the legacy contract exposes; :mod:`.identity` reads it.
        self.auth_context = auth_context
        self.state = SessionState.CREATED
        self._heartbeat: HeartbeatNegotiation | None = None
        self._negotiated_era: str | None = None

    # ── reporting ────────────────────────────────────────────────

    @property
    def era_report(self) -> EraReport:
        if self._negotiated_era is None:
            return UNNEGOTIATED
        heartbeat = self._heartbeat
        return EraReport(
            mcp_protocol_era=self._negotiated_era,
            extension_version=heartbeat.extension_version if heartbeat else None,
            heartbeat_supported=bool(heartbeat and heartbeat.supported),
        )

    @property
    def heartbeat_ready(self) -> bool:
        return self.state is SessionState.READY and self.era_report.heartbeat_supported

    def capabilities(self) -> dict[str, Any]:
        from mcp_heartbeat.model import EXTENSION_VERSION

        from .era import HEARTBEAT_CAPABILITY, HEARTBEAT_NAMESPACE

        if not self.request_heartbeat:
            return {}
        return {
            HEARTBEAT_NAMESPACE: {
                HEARTBEAT_CAPABILITY: {"extension_version": EXTENSION_VERSION}
            }
        }

    # ── the handshake, client side ───────────────────────────────

    def initialize_request(self) -> tuple[str, dict[str, Any]]:
        """The ``initialize`` request to put on the wire."""
        if self.state is not SessionState.CREATED:
            raise LegacyProtocolError(
                LegacyReason.LIFECYCLE_OUT_OF_ORDER,
                f"{INITIALIZE} already sent (state={self.state.value})",
            )
        return INITIALIZE, {
            "protocolVersion": self.requested_protocol_version,
            "capabilities": self.capabilities(),
            "clientInfo": {"name": self.client_name, "version": "0.1.0"},
        }

    def consume_initialize_result(self, result: Mapping[str, Any]) -> EraReport:
        """Accept the server's answer, or refuse a downgrade.

        A server that answers with any revision other than the one we asked
        for is refused outright. The legacy contract lets a server counter-
        offer; this adapter declines to guess whether the counter-offer was
        cooperative or an attacker steering us onto weaker semantics.
        """
        answered = result.get("protocolVersion")
        if answered != self.requested_protocol_version:
            self.state = SessionState.FAILED
            raise LegacyProtocolError(
                LegacyReason.SILENT_DOWNGRADE_REFUSED,
                f"server answered protocolVersion {answered!r}; this client "
                f"offered {self.requested_protocol_version!r} and will not be "
                "moved to a revision it did not choose",
                code=JsonRpcCode.INVALID_PARAMS,
            )
        self._negotiated_era = assert_legacy_era(answered)
        # Bilateral, as on the server side: a client that offered nothing does
        # not acquire the extension just because the server advertises it.
        self._heartbeat = (
            negotiate_heartbeat(result.get("capabilities"))
            if self.request_heartbeat
            else _not_offered("this client did not offer the heartbeat extension")
        )
        self.state = SessionState.INITIALIZING
        return self.era_report

    def initialized_notification(self) -> tuple[str, dict[str, Any]]:
        """The notification the archived client never sent (D-03)."""
        if self.state is not SessionState.INITIALIZING:
            raise LegacyProtocolError(
                LegacyReason.LIFECYCLE_OUT_OF_ORDER,
                f"{INITIALIZED_NOTIFICATION} follows a successful "
                f"{INITIALIZE} (state={self.state.value})",
            )
        self.state = SessionState.READY
        return INITIALIZED_NOTIFICATION, {}


__all__ = [
    "INITIALIZED_NOTIFICATION",
    "INITIALIZE",
    "Disagreement",
    "LegacyClientSession",
    "LegacyServerSession",
    "SessionState",
]
