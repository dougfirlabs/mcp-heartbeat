"""Authoritative refetch, and why delivery can never be load-bearing.

A change hint says *go look*. Only the refetched document decides anything.
That single rule is what makes every delivery fault below a performance
question rather than a correctness question — a consumer that receives no
hint at all still converges, because its refetch deadline is derived from
the **held lease's own expiry**, not from the stream.

The deliberate consequence is that this module is boring under fault. Lost,
duplicated, reordered, forged and coalesced hints all funnel into the same
call, and the actual judgement is the portable core's ``admit()``, which is
already a pure function with its own fixture corpus. Nothing here re-decides
what the core decided; it decides only *when to ask* and *what the answer
means for this topology*.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from mcp_heartbeat.clock import Clock
from mcp_heartbeat.errors import HeartbeatError, ViolationCode
from mcp_heartbeat.lineage import DEFAULT_MAX_SKEW_SECONDS, LineageState, admit
from mcp_heartbeat.model import Heartbeat, IdentityBinding
from mcp_heartbeat.ports import ChangeHint

from .errors import IdentityUnbound
from .identity import Principal, RequestIdentityBinder, ResponseBinding, enforce

#: Fraction of the *remaining* lease window at which an idle consumer
#: refetches. Below 1.0 so a refetch is attempted while the held lease is
#: still valid, which is what turns a single lost hint into a non-event.
DEFAULT_REFETCH_FRACTION = 0.5

#: Never wait longer than this between refetches, however long the lease.
DEFAULT_MAX_REFETCH_INTERVAL_SECONDS = 60.0


class Convergence(str, Enum):
    """What one refetch established. Exhaustive and mutually exclusive."""

    #: A newer revision was fetched and admitted.
    ADVANCED = "advanced"
    #: The authoritative document is the one already held. Idempotent.
    UNCHANGED = "unchanged"
    #: The document was refused by the core; the held lease is preserved.
    REFUSED = "refused"
    #: An authenticated principal claimed a participant it may not publish.
    IDENTITY_UNBOUND = "identity_unbound"
    #: Readable, but no principal evidence — retained, never authoritative.
    NON_AUTHORITATIVE = "non_authoritative"
    #: The fetch itself failed. Says nothing about the participant.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class FetchResult:
    """One authoritative ``resources/read``, with its request's principal.

    The principal travels *with* the response rather than being read from
    ambient state, so a gateway multiplexing many participants cannot end up
    binding all of them to whichever principal happened to authenticate last.
    """

    document: Mapping[str, Any]
    principal: Principal | None = None


@runtime_checkable
class AuthoritativeSource(Protocol):
    """Transport → consumer. Fetches the lease and reports who served it."""

    def fetch(self, participant_id: str) -> FetchResult:
        ...


#: Builds a per-request identity binder from that request's principal.
#: A *factory*, not a binder, because the whole point of D-05's repair is
#: that a binder is scoped to one request — handing the consumer a
#: long-lived binder would put the channel-level shape straight back.
BinderFactory = Callable[[Principal | None], RequestIdentityBinder]


@dataclass(frozen=True)
class ConvergenceVerdict:
    """The outcome of one refetch, and the state it left behind."""

    convergence: Convergence
    participant_id: str
    binding: ResponseBinding | None = None
    reason: ViolationCode | str | None = None
    held: Heartbeat | None = None

    @property
    def authoritative(self) -> bool:
        return self.convergence is Convergence.ADVANCED

    def to_dict(self) -> dict[str, Any]:
        return {
            "convergence": self.convergence.value,
            "participant_id": self.participant_id,
            "reason": str(self.reason) if self.reason is not None else None,
            "identity_binding": self.binding.binding.value if self.binding else None,
            "held_revision": self.held.revision if self.held else None,
        }


class HeartbeatConsumer:
    """Tracks one participant's lease across an unreliable hint channel.

    Not thread-safe and deliberately so: it owns no lock, no thread and no
    timer. A caller drives it from whatever loop it already has, which is
    what keeps it testable on a ``FakeClock`` with nothing sleeping.
    """

    def __init__(
        self,
        participant_id: str,
        source: AuthoritativeSource,
        clock: Clock,
        *,
        binder_factory: BinderFactory | None = None,
        max_skew_seconds: float = DEFAULT_MAX_SKEW_SECONDS,
        refetch_fraction: float = DEFAULT_REFETCH_FRACTION,
        max_refetch_interval_seconds: float = DEFAULT_MAX_REFETCH_INTERVAL_SECONDS,
    ) -> None:
        self.participant_id = participant_id
        self._source = source
        self._clock = clock
        self._binder_factory = binder_factory
        self._max_skew_seconds = max_skew_seconds
        self._refetch_fraction = refetch_fraction
        self._max_refetch_interval = max_refetch_interval_seconds

        self.state = LineageState(participant_id=participant_id)
        self.last_binding: ResponseBinding | None = None
        #: Set by a hint, cleared by a refetch. A single flag, not a queue:
        #: coalescing is the backpressure strategy, and N hints between two
        #: refetches must cost exactly one refetch.
        self.refetch_pending = False
        self._last_fetch_at: datetime | None = None
        #: Counters, for evidence. They never influence a decision.
        self.hints_seen = 0
        self.hints_coalesced = 0
        self.fetches = 0

    # ── held state ────────────────────────────────────────────────

    @property
    def held(self) -> Heartbeat | None:
        return self.state.held

    def is_fresh(self, now: datetime | None = None) -> bool:
        now = now if now is not None else self._clock.now()
        return self.held is not None and self.held.is_fresh(now)

    def refetch_deadline(self) -> datetime | None:
        """When this consumer must refetch, derived from the held lease alone.

        ``None`` before anything is held — the caller fetches immediately.
        Crucially this reads no hint state: that is what "loss tolerant"
        means operationally.
        """
        held = self.held
        if held is None or self._last_fetch_at is None:
            return None
        window = (held.expires_at - self._last_fetch_at).total_seconds()
        interval = min(max(window, 0.0) * self._refetch_fraction, self._max_refetch_interval)
        return self._last_fetch_at + timedelta(seconds=interval)

    def due(self, now: datetime | None = None) -> bool:
        """Whether a refetch is owed, by hint or by deadline."""
        now = now if now is not None else self._clock.now()
        if self.refetch_pending or self.held is None:
            return True
        deadline = self.refetch_deadline()
        return deadline is None or now >= deadline

    # ── hints ─────────────────────────────────────────────────────

    def on_hint(self, hint: ChangeHint | None = None) -> bool:
        """Record a change hint. Returns whether it scheduled a new refetch.

        A hint that matches the held revision is *not* discarded silently
        — it still counts — but it schedules nothing, which is what makes
        duplicate redelivery free rather than merely harmless.
        """
        self.hints_seen += 1
        if hint is not None and self.held is not None and hint.matches(self.held):
            return False
        if self.refetch_pending:
            self.hints_coalesced += 1
            return False
        self.refetch_pending = True
        return True

    # ── the authoritative path ────────────────────────────────────

    def refetch(self, now: datetime | None = None) -> ConvergenceVerdict:
        """Fetch the authoritative lease and reconcile it. Never raises."""
        now = now if now is not None else self._clock.now()
        self.refetch_pending = False
        self.fetches += 1

        try:
            result = self._source.fetch(self.participant_id)
        except Exception as exc:
            # Unreachable says nothing about the participant, so the held
            # lease stands until its own expiry decides for us.
            return ConvergenceVerdict(
                Convergence.UNREACHABLE,
                self.participant_id,
                reason=type(exc).__name__,
                held=self.held,
            )

        self._last_fetch_at = now
        binding = self._bind(result)
        self.last_binding = binding

        if binding is not None:
            try:
                enforce(binding)
            except IdentityUnbound:
                return ConvergenceVerdict(
                    Convergence.IDENTITY_UNBOUND,
                    self.participant_id,
                    binding=binding,
                    reason="principal_not_permitted",
                    held=self.held,
                )
            if binding.binding is not IdentityBinding.BOUND:
                # Readable and retained for observability, but it may not
                # move held state. Freshness is not permission.
                return ConvergenceVerdict(
                    Convergence.NON_AUTHORITATIVE,
                    self.participant_id,
                    binding=binding,
                    reason=binding.reason,
                    held=self.held,
                )

        try:
            admission = admit(
                self.state, result.document, now, max_skew_seconds=self._max_skew_seconds
            )
        except HeartbeatError as exc:
            return ConvergenceVerdict(
                Convergence.REFUSED,
                self.participant_id,
                binding=binding,
                reason=exc.code,
                held=self.held,
            )

        self.state = admission.state
        if admission.duplicate:
            return ConvergenceVerdict(
                Convergence.UNCHANGED, self.participant_id, binding=binding, held=self.held
            )
        if admission.reason is not None:
            return ConvergenceVerdict(
                Convergence.REFUSED,
                self.participant_id,
                binding=binding,
                reason=admission.reason,
                held=self.held,
            )
        return ConvergenceVerdict(
            Convergence.ADVANCED, self.participant_id, binding=binding, held=self.held
        )

    def poll(self, now: datetime | None = None) -> ConvergenceVerdict | None:
        """Refetch iff one is owed. The whole consumer loop, in one call."""
        now = now if now is not None else self._clock.now()
        return self.refetch(now) if self.due(now) else None

    def _bind(self, result: FetchResult) -> ResponseBinding | None:
        if self._binder_factory is None:
            return None
        binder = self._binder_factory(result.principal)
        if not isinstance(binder, RequestIdentityBinder):  # pragma: no cover - defensive
            raise TypeError("binder_factory must return a RequestIdentityBinder")
        return binder.evaluate(self.participant_id)


# ── deployment topologies ─────────────────────────────────────────────


@dataclass(frozen=True)
class TopologyRule:
    """A deployment shape and the verdict this adapter is pinned to give.

    The PRD requires *deterministic* behaviour for each of these, which is
    a stronger claim than "handled": the table below is asserted by
    ``tests/current/test_delivery_faults.py``, so a refactor that quietly
    changes one of these outcomes fails a test that names the topology.
    """

    topology: str
    situation: str
    rule: str
    verdict: Convergence
    reason: str | None = None


TOPOLOGY_RULES: tuple[TopologyRule, ...] = (
    TopologyRule(
        "round_robin_replicas",
        "Two live replicas answer alternate reads under one participant id.",
        "Each replica has its own epoch. The first flap back to an already-retired "
        "epoch is refused, surfacing the split identity instead of alternating "
        "between two lease streams that each look plausible on its own.",
        Convergence.REFUSED,
        ViolationCode.BOOT_ID_REUSE.value,
    ),
    TopologyRule(
        "gateway_termination",
        "One authenticated channel carries many participants.",
        "Binding is computed per response from that request's principal, so a "
        "gateway cannot collapse many participants onto one identity.",
        Convergence.ADVANCED,
        None,
    ),
    TopologyRule(
        "serverless_cold_start",
        "The participant restarts with a new epoch and sequence 0.",
        "A previously unseen epoch is admitted normally; a cold start is a new "
        "lease stream, not a rollback.",
        Convergence.ADVANCED,
        None,
    ),
    TopologyRule(
        "rolling_deployment",
        "An old instance keeps publishing after a new one is accepted.",
        "The old epoch is retired the moment the new one is admitted, so its "
        "continued leases are refused rather than interleaved.",
        Convergence.REFUSED,
        ViolationCode.BOOT_ID_REUSE.value,
    ),
    TopologyRule(
        "asymmetric_connectivity",
        "Hints arrive but the authoritative read fails.",
        "Nothing is fabricated from the hint. The held lease stands and expires "
        "on its own schedule.",
        Convergence.UNREACHABLE,
        None,
    ),
    TopologyRule(
        "backpressure",
        "Hints arrive faster than refetches complete.",
        "Hints coalesce onto one pending refetch, so N hints cost one read and "
        "correctness is independent of the drop rate.",
        Convergence.ADVANCED,
        None,
    ),
)

TOPOLOGY_BY_NAME: Mapping[str, TopologyRule] = {rule.topology: rule for rule in TOPOLOGY_RULES}


__all__ = [
    "DEFAULT_MAX_REFETCH_INTERVAL_SECONDS",
    "DEFAULT_REFETCH_FRACTION",
    "AuthoritativeSource",
    "Convergence",
    "ConvergenceVerdict",
    "FetchResult",
    "HeartbeatConsumer",
    "TOPOLOGY_BY_NAME",
    "TOPOLOGY_RULES",
    "TopologyRule",
]
