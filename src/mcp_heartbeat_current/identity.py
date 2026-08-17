"""Binding a claimed participant to the authenticated principal — per response.

HB-00 defect D-05: the legacy path carried one ``transport_authenticated``
flag per *channel*. Behind a gateway, one authenticated channel therefore
reported "authenticated" for every participant multiplexed over it, so a
tenant that proved who it was could publish a lease for anyone sharing its
connection. The repair is structural, not a stricter check: **binding is
computed per response, from the principal that authenticated that specific
request**, and there is no channel-level flag in this module to regress to.

Three operator decisions shape the surface:

* **D-N2** — ``identity_binding`` is its own three-valued facet
  (``bound`` / ``unbound`` / ``unverified``), *not* a rung on a verification
  ladder. A ladder invites "at least level 2", which silently promotes
  ``unverified``; three named states do not compare.
* **D-N3** — the principal→permitted-participant mapping is owned by
  a presence service or another deployment owner and is **injected**. This
  module holds no policy table and no default allow rule.
* **D-N7** — the binding shape is implemented once, here, and reused by
  every consumer facet rather than reimplemented at integration time.
* **D2** (HB-X1) — an *authenticated* principal the injected policy does not
  cover resolves to ``unbound``, not ``unverified``. This module used to
  answer ``unverified`` there while the legacy adapter answered ``unbound``:
  neither granted authority, but a consumer that alerts on ``unbound`` would
  page on one era and stay silent on the other for the identical
  misconfiguration. The operator settled it as fail-closed in both eras.
  ``unverified`` now means exactly one thing — *no principal at all* — which
  is the only case where an absence of evidence is genuinely an absence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from mcp_heartbeat.model import IdentityBinding, IdentityClaim

from .errors import IdentityUnbound


@dataclass(frozen=True)
class Principal:
    """Who authenticated *this request*.

    The triple mirrors the official SDK's ``principal_components``
    (``client_id``, ``issuer``, ``subject``) so a deployment can hand this
    module the SDK's own value without reshaping it.
    """

    client_id: str | None = None
    issuer: str | None = None
    subject: str | None = None

    @property
    def is_empty(self) -> bool:
        """True when nothing identifying was presented."""
        return not any((self.client_id, self.issuer, self.subject))

    def compact(self) -> str:
        """A stable, comparable rendering. Never a credential."""
        return json.dumps(
            [self.client_id, self.issuer, self.subject], separators=(",", ":"), sort_keys=True
        )

    @classmethod
    def from_compact(cls, raw: str | None) -> "Principal | None":
        """Parse what :meth:`compact` produced; ``None`` stays ``None``."""
        if raw is None:
            return None
        try:
            client_id, issuer, subject = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return cls(client_id=client_id, issuer=issuer, subject=subject)


@runtime_checkable
class PermittedParticipants(Protocol):
    """The injected policy seam (D-N3).

    Returns ``True`` when ``principal`` may publish ``participant_id``,
    ``False`` when it explicitly may not, and ``None`` when the deployment
    has no opinion.

    The seam keeps all three answers — "no opinion" is genuinely different
    from "refused", and collapsing them here would destroy a distinction
    the diagnostics still report. What ``None`` does *not* do is soften the
    outcome: for an authenticated principal it produces ``unbound``, which
    fails closed (operator decision D2). Silence is not consent, and the
    binder — not the policy — is where that is decided.
    """

    def permits(self, principal: Principal, participant_id: str) -> bool | None:
        ...


@dataclass(frozen=True)
class ResponseBinding:
    """The ``identity_binding`` facet for one response.

    Carried *beside* the heartbeat payload, never inside it: the lease is
    six fields and admitting a seventh would make the document's own
    validity depend on the transport that carried it.
    """

    binding: IdentityBinding
    participant_id: str
    principal_fingerprint: str | None = None
    reason: str | None = None

    @property
    def authoritative(self) -> bool:
        """Whether this response may update a consumer's held lease.

        Only ``bound`` is authoritative. ``unverified`` is readable and may
        be retained for observability, but it can never promote itself into
        evidence of presence — that promotion is exactly what D-05 was.
        """
        return self.binding is IdentityBinding.BOUND

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_binding": self.binding.value,
            "participant_id": self.participant_id,
            "principal_fingerprint": self.principal_fingerprint,
            "authoritative": self.authoritative,
            "reason": self.reason,
        }


class RequestIdentityBinder:
    """Resolves one claim against one request's authenticated principal.

    Satisfies the portable core's ``IdentityBinder`` port structurally, so
    the core still never learns that MCP exists.

    Construct one **per request**. The constructor takes the principal by
    value for that reason: there is no setter, no channel default and no
    mutable "current principal", so the shape that produced D-05 cannot be
    rebuilt out of this class by accident.
    """

    def __init__(
        self,
        policy: PermittedParticipants,
        principal: Principal | None,
    ) -> None:
        self._policy = policy
        self._principal = principal

    @property
    def principal(self) -> Principal | None:
        return self._principal

    def bind(self, claim: IdentityClaim) -> IdentityBinding:
        """Report how strongly ``claim`` is tied to the request's principal."""
        return self.evaluate(claim.participant_id).binding

    def evaluate(self, participant_id: str) -> ResponseBinding:
        """The full facet, including why."""
        principal = self._principal
        if principal is None or principal.is_empty:
            # No principal evidence at all. Not a failure — an unauthenticated
            # read of a public lease is legitimate — but never authoritative.
            return ResponseBinding(
                binding=IdentityBinding.UNVERIFIED,
                participant_id=participant_id,
                reason="no_principal_evidence",
            )

        verdict = self._policy.permits(principal, participant_id)
        fingerprint = principal.compact()
        if verdict is True:
            return ResponseBinding(
                binding=IdentityBinding.BOUND,
                participant_id=participant_id,
                principal_fingerprint=fingerprint,
            )
        if verdict is False:
            return ResponseBinding(
                binding=IdentityBinding.UNBOUND,
                participant_id=participant_id,
                principal_fingerprint=fingerprint,
                reason="principal_not_permitted",
            )
        # The deployment declined to answer about a principal that *did*
        # authenticate. Silence is not consent — and per operator decision
        # D2 it is not an absence of evidence either, because there is no
        # absence here: something proved who it was and then claimed a
        # participant the policy does not cover it for. That fails closed,
        # exactly as the legacy era already did, so the two eras give one
        # observable answer instead of two.
        #
        # Note where the distinction survives: the *binding* is `unbound`
        # either way, but the reason still separates "explicitly refused"
        # from "policy said nothing". The observable answer is normalized;
        # the diagnostic is not flattened with it.
        return ResponseBinding(
            binding=IdentityBinding.UNBOUND,
            participant_id=participant_id,
            principal_fingerprint=fingerprint,
            reason="no_policy_for_principal",
        )


def enforce(binding: ResponseBinding) -> ResponseBinding:
    """Fail closed on ``unbound``; pass everything else through.

    ``unbound`` is the only state that raises, because it is the only one
    where something authenticated and *then* claimed an identity it does not
    hold. A missing principal is an absence of evidence; a refused principal
    is evidence of a problem.
    """
    if binding.binding is IdentityBinding.UNBOUND:
        raise IdentityUnbound(
            f"principal may not publish participant {binding.participant_id!r}",
            data=binding.to_dict(),
        )
    return binding


class StaticPermittedParticipants:
    """A concrete policy for tests and single-tenant deployments.

    Emphatically *not* the adapter's policy table — it is a mapping the
    caller builds and injects. Anything not listed yields ``None`` rather
    than ``False``, so "this deployment has no opinion" stays
    distinguishable from "this deployment said no" in the diagnostics.

    Both answers refuse. Under D2 an authenticated principal the map does
    not cover resolves to ``unbound``, so an incomplete map fails closed;
    it is only the *reason* that tells an operator whether they hit a
    policy or a hole in one.
    """

    def __init__(self, mapping: Mapping[str, frozenset[str] | set[str] | tuple[str, ...]]) -> None:
        self._mapping = {k: frozenset(v) for k, v in mapping.items()}

    def permits(self, principal: Principal, participant_id: str) -> bool | None:
        allowed = self._mapping.get(principal.compact())
        if allowed is None:
            return None
        return participant_id in allowed


__all__ = [
    "PermittedParticipants",
    "Principal",
    "RequestIdentityBinder",
    "ResponseBinding",
    "StaticPermittedParticipants",
    "enforce",
]
