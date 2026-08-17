"""Authoritative fetch, and change hints that stop lying. Defect D-10.

The archived implementation emitted ``{uri, revision, digest}`` under the
method name ``notifications/resources/updated``, whose legacy params are
``{uri}`` alone. Both ends of the lab spoke that dialect, so 12/12 scenarios
passed — while the forged-notification defence, which compares a hint's digest
against the refetched lease, silently evaluated to nothing against any
conformant peer. A ``{uri}``-only hint carries no digest, so there was nothing
to compare and no violation code to say so.

Two repairs, and the order matters:

1. **The standard method carries standard params.** ``updated_notifications``
   emits ``{uri}`` under ``notifications/resources/updated`` and puts revision
   metadata in a *separate, namespaced* extension notification that only a peer
   which negotiated the extension will receive. Nothing is overloaded.
2. **The defence no longer lives in the hint.** A hint's single legal effect is
   to schedule a refetch; correctness comes from
   :func:`mcp_heartbeat.lineage.admit` over the refetched document, which a
   forged hint cannot influence. Digest comparison survives as
   *corroboration* — reported as ``True``/``False``/``None`` — and ``None``
   is now counted rather than silently ignored.

Consequence worth stating: metadata arriving under the *standard* method name
is dropped, not trusted, because a conformant peer's ``{uri}`` and the lab's
overloaded payload are indistinguishable in intent. The drop is flagged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from mcp_heartbeat.errors import ViolationCode
from mcp_heartbeat.lineage import Admission, LineageState, admit
from mcp_heartbeat.model import Heartbeat, IdentityBinding, IdentityClaim, is_digest
from mcp_heartbeat.ports import ChangeHint, HeartbeatSource

from .era import EraReport
from .errors import JsonRpcCode, LegacyProtocolError, LegacyReason
from .identity import IdentityBindingEvidence, unverified_evidence
from .session import LegacyClientSession

#: Legacy addressing for a participant's heartbeat resource.
URI_SCHEME = "presence"

#: The standard legacy notification. Params are ``{uri}`` and nothing else.
STANDARD_UPDATED_METHOD = "notifications/resources/updated"

#: The namespaced extension notification that may carry revision metadata.
#: A peer that did not negotiate the extension never sees it, and a peer that
#: does not understand it answers a notification with silence — which is the
#: correct legacy behaviour rather than a failure.
EXTENDED_UPDATED_METHOD = "notifications/experimental/presenceLease/updated"


def heartbeat_uri(participant_id: str) -> str:
    """The legacy resource address for ``participant_id``."""
    return f"{URI_SCHEME}://{participant_id}"


def participant_from_uri(uri: object) -> str:
    """Inverse of :func:`heartbeat_uri`. Raises on anything else."""
    prefix = f"{URI_SCHEME}://"
    if not isinstance(uri, str) or not uri.startswith(prefix) or len(uri) == len(prefix):
        raise LegacyProtocolError(
            LegacyReason.MALFORMED_HINT,
            f"{uri!r} is not a {prefix} heartbeat address",
            code=JsonRpcCode.INVALID_PARAMS,
        )
    return uri[len(prefix) :]


@dataclass(frozen=True)
class LegacyHint:
    """A parsed change hint. Advisory by construction.

    ``revision`` and ``digest`` are ``None`` for a conformant standard hint,
    and that is the ordinary case rather than a degraded one — see this
    module's docstring.
    """

    address: str
    method: str
    revision: str | None = None
    digest: str | None = None
    #: Set when metadata arrived under the standard method name and was
    #: dropped. Exists so D-10's degradation is visible instead of silent.
    overloaded_standard_method: bool = False

    @property
    def carries_revision_metadata(self) -> bool:
        return self.revision is not None and self.digest is not None

    @property
    def participant_id(self) -> str:
        return participant_from_uri(self.address)

    def to_core_hint(self) -> ChangeHint:
        """Lift to the core's :class:`~mcp_heartbeat.ports.ChangeHint`.

        Only legal when metadata is present; the core's hint type requires a
        digest, and inventing one here is exactly the kind of quiet fabrication
        D-10 is about.
        """
        if not self.carries_revision_metadata:
            raise LegacyProtocolError(
                LegacyReason.MALFORMED_HINT,
                "a standard legacy hint carries no revision metadata and cannot "
                "become a core ChangeHint; refetch instead",
                code=JsonRpcCode.INVALID_PARAMS,
            )
        return ChangeHint(
            address=self.address, revision=str(self.revision), digest=str(self.digest)
        )


def updated_notifications(
    heartbeat: Heartbeat, *, extended: bool
) -> list[tuple[str, dict[str, Any]]]:
    """Notifications to emit for a new revision of ``heartbeat``.

    Always the standard ``{uri}`` one. The namespaced extension notification
    is appended only when the peer negotiated the extension.
    """
    address = heartbeat_uri(heartbeat.participant_id)
    out: list[tuple[str, dict[str, Any]]] = [(STANDARD_UPDATED_METHOD, {"uri": address})]
    if extended:
        out.append(
            (
                EXTENDED_UPDATED_METHOD,
                {
                    "uri": address,
                    "revision": heartbeat.revision,
                    "digest": heartbeat.digest,
                },
            )
        )
    return out


def parse_updated(method: str, params: Mapping[str, Any] | None) -> LegacyHint:
    """Parse an updated-notification into a :class:`LegacyHint`."""
    params = params or {}
    if method == STANDARD_UPDATED_METHOD:
        address = params.get("uri")
        participant_from_uri(address)  # validates, raises on malformed
        overloaded = "revision" in params or "digest" in params
        return LegacyHint(
            address=str(address),
            method=method,
            overloaded_standard_method=overloaded,
        )

    if method == EXTENDED_UPDATED_METHOD:
        address = params.get("uri")
        participant_from_uri(address)
        revision = params.get("revision")
        digest = params.get("digest")
        if not isinstance(revision, str) or not revision:
            raise LegacyProtocolError(
                LegacyReason.MALFORMED_HINT,
                f"{EXTENDED_UPDATED_METHOD} requires a revision string",
                code=JsonRpcCode.INVALID_PARAMS,
            )
        if not is_digest(digest):
            raise LegacyProtocolError(
                LegacyReason.MALFORMED_HINT,
                f"{EXTENDED_UPDATED_METHOD} requires a 'sha256:<64 hex>' digest",
                code=JsonRpcCode.INVALID_PARAMS,
            )
        return LegacyHint(
            address=str(address), method=method, revision=revision, digest=str(digest)
        )

    raise LegacyProtocolError(
        LegacyReason.MALFORMED_HINT,
        f"{method!r} is not a heartbeat change notification",
        code=JsonRpcCode.METHOD_NOT_FOUND,
    )


class LegacyRefusal(str, Enum):
    """Adapter-level reasons a fetch was refused.

    Separate from :class:`mcp_heartbeat.errors.ViolationCode` on purpose: those
    describe the *document*, these describe the *publisher* and the *session*.
    Merging them is precisely the "fold publisher identity into content
    verification" mistake the hard constraints forbid.
    """

    IDENTITY_UNBOUND = "identity_unbound"
    HEARTBEAT_NOT_NEGOTIATED = "heartbeat_not_negotiated"


@dataclass(frozen=True)
class LegacyFetchOutcome:
    """Everything one authoritative fetch decided, kept in separate fields.

    ``admission`` answers "is this document a valid next revision?".
    ``identity`` answers "may this publisher speak for that participant?".
    They are reported apart, and a reader can see both — a document can be
    perfectly valid and still refused because the publisher was not permitted.
    """

    admission: Admission
    identity: IdentityBindingEvidence
    era: EraReport
    #: ``True``/``False`` when a hint carried a digest to corroborate;
    #: ``None`` when there was no hint or no digest to compare.
    hint_corroborated: bool | None = None
    refused: LegacyRefusal | None = None

    @property
    def accepted(self) -> bool:
        """Admitted *and* not refused at the adapter layer."""
        return self.refused is None and self.admission.accepted

    @property
    def duplicate(self) -> bool:
        """Byte-identical redelivery: idempotent, neither accepted nor an error.

        Surfaced separately because a poller sees this constantly, and
        ``accepted is False`` with no reason would read as a silent failure.
        """
        return self.refused is None and self.admission.duplicate

    @property
    def reason(self) -> str | None:
        if self.refused is not None:
            return self.refused.value
        code: ViolationCode | None = self.admission.reason
        return code.value if code is not None else None


class LegacyHeartbeatConsumer:
    """Reads authoritative heartbeats over a legacy MCP session.

    Holds the lineage state for exactly one participant and refetches through
    an injected :class:`~mcp_heartbeat.ports.HeartbeatSource`. A hint never
    updates state on its own — it is an argument to :meth:`refetch`, and its
    only effect is corroboration after the fact.
    """

    def __init__(
        self,
        *,
        participant_id: str,
        source: HeartbeatSource,
        session: LegacyClientSession,
        binder: Any = None,
    ) -> None:
        self.participant_id = participant_id
        self.source = source
        self.session = session
        self.binder = binder
        self.state = LineageState(participant_id=participant_id)
        #: Hints that could not be corroborated because they carried no
        #: digest. D-10 made this number invisible; now it is countable.
        self.uncorroborated_hints = 0

    @property
    def held(self) -> Heartbeat | None:
        return self.state.held

    def address(self) -> str:
        return heartbeat_uri(self.participant_id)

    def _evidence(self, claimed_participant: str, epoch_id: str) -> IdentityBindingEvidence:
        if self.binder is None:
            return unverified_evidence(claimed_participant)
        return self.binder.evidence(
            IdentityClaim(participant_id=claimed_participant, epoch_id=epoch_id)
        )

    def refetch(self, now: datetime, *, hint: LegacyHint | None = None) -> LegacyFetchOutcome:
        """Fetch the authoritative document and decide whether to hold it.

        Fails closed on a session that never negotiated the extension, and on
        a publisher the injected mapping does not permit. Neither refusal
        changes the held lease.
        """
        era = self.session.era_report
        if not self.session.heartbeat_ready:
            return LegacyFetchOutcome(
                admission=Admission(self.state, ViolationCode.SCHEMA_INVALID),
                identity=unverified_evidence(self.participant_id),
                era=era,
                refused=LegacyRefusal.HEARTBEAT_NOT_NEGOTIATED,
            )

        document = self.source.fetch(self.participant_id)
        outcome = admit(self.state, document, now)

        claimed = document.get("node_id") if isinstance(document, Mapping) else None
        epoch = document.get("boot_id") if isinstance(document, Mapping) else None
        evidence = self._evidence(str(claimed or self.participant_id), str(epoch or ""))

        if evidence.binding is IdentityBinding.UNBOUND:
            # Fail closed and keep the previous lease: a document from a
            # publisher we do not accept must not become held state, however
            # well-formed it is.
            return LegacyFetchOutcome(
                admission=outcome,
                identity=evidence,
                era=era,
                refused=LegacyRefusal.IDENTITY_UNBOUND,
            )

        corroborated: bool | None = None
        if outcome.accepted or outcome.duplicate:
            held = outcome.state.held
            if hint is not None and held is not None:
                if hint.carries_revision_metadata:
                    corroborated = hint.digest == held.digest
                else:
                    self.uncorroborated_hints += 1

        if outcome.accepted:
            self.state = outcome.state

        return LegacyFetchOutcome(
            admission=outcome,
            identity=evidence,
            era=era,
            hint_corroborated=corroborated,
        )


__all__ = [
    "EXTENDED_UPDATED_METHOD",
    "STANDARD_UPDATED_METHOD",
    "URI_SCHEME",
    "LegacyFetchOutcome",
    "LegacyHeartbeatConsumer",
    "LegacyHint",
    "LegacyRefusal",
    "heartbeat_uri",
    "parse_updated",
    "participant_from_uri",
    "updated_notifications",
]
