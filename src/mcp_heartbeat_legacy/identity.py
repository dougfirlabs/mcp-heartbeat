"""Bind a claimed participant to the strongest principal legacy MCP exposes.

The legacy contract has no per-message signer. The strongest authenticated
context it offers is the **session** one: whoever the transport authenticated
when the connection was established, which then carries every request on that
connection. So that is what this adapter binds to, and the evidence names it
explicitly rather than implying something stronger.

Three rules, all from the HB-00 disposition and operator decisions D-N2/D-N3:

* the result is a separate ``bound`` / ``unbound`` / ``unverified`` facet, not
  a rung on the content-verification ladder — publisher identity and document
  trustworthiness are different questions and collapsing them overstates both
  (D-N2);
* the principal → permitted-participant mapping is **injected** by the
  deployment owner (a presence service, or whoever runs the adapter). There is
  no default and no policy table in this package (D-N3);
* absence of a principal is ``unverified``, never a promotion. "The socket was
  TLS" is not an identity (defect D-05).

Operator decision **D2** (HB-X1) adds a fourth: an authenticated principal the
injected mapping does not cover resolves to ``unbound`` — fail closed — which
is what this adapter already did, and is now what the current-era adapter does
too. The eras give one observable answer to a policy gap rather than two. The
*reason* still separates "refused" from "not covered", because those are the
same symptom with opposite fixes; it travels in the evidence record, never in
the six-field wire document.

Stdlib plus the portable core.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Collection, Mapping

from mcp_heartbeat.model import IdentityBinding, IdentityClaim

#: What the session context was derived from. Recorded verbatim in the
#: evidence so a reader can judge the strength of the binding rather than
#: taking a three-valued enum on faith.
NO_CONTEXT = "none"


@dataclass(frozen=True)
class LegacySessionAuthContext:
    """The authenticated principal for one legacy MCP session.

    ``principal`` is ``None`` when the transport authenticated nobody — an
    anonymous stdio pipe, a plaintext socket, or a gateway that terminated
    authentication and forwarded no assertion. That case is ``unverified``,
    and the *reason* is preserved in :attr:`source`.
    """

    principal: str | None = None
    #: e.g. ``"session_token"``, ``"mtls_peer"``, ``"oauth_subject"``.
    source: str = NO_CONTEXT

    @property
    def authenticated(self) -> bool:
        return self.principal is not None


#: Injected by the deployment owner. Either a mapping principal → permitted
#: participant ids, or a predicate. Never defaulted inside this package.
PermittedParticipants = (
    Mapping[str, Collection[str]] | Callable[[str, str], bool]
)


@dataclass(frozen=True)
class IdentityBindingEvidence:
    """Why the binding came out the way it did.

    An acceptance criterion of this PRD is that the evidence *names the legacy
    session authentication context used*, so ``context_source`` is part of the
    record and not merely an implementation detail.
    """

    binding: IdentityBinding
    participant_id: str
    principal: str | None
    context_source: str
    detail: str
    #: A short, stable code naming *why*, sharing its vocabulary with the
    #: current adapter's ``ResponseBinding.reason``. ``detail`` stays the
    #: human sentence; this is the part a consumer can branch on without
    #: parsing prose. Both live here, beside the six-field wire document,
    #: and neither is ever a member of it.
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "identity_binding": self.binding.value,
            "participant_id": self.participant_id,
            "principal": self.principal,
            "context_source": self.context_source,
            "detail": self.detail,
            "reason": self.reason,
        }


class LegacySessionIdentityBinder:
    """Resolve a heartbeat's claimed participant against the session principal.

    Satisfies :class:`mcp_heartbeat.ports.IdentityBinder` structurally, so the
    core never learns that MCP sessions exist.
    """

    def __init__(
        self,
        *,
        context: LegacySessionAuthContext,
        permitted: PermittedParticipants,
    ) -> None:
        # `permitted` is required, positionally unavoidable, and has no
        # default. Making it optional would be the first step towards this
        # package owning a policy table, which D-N3 assigns elsewhere.
        self.context = context
        self._permitted = permitted

    def _is_permitted(self, principal: str, participant_id: str) -> bool:
        if callable(self._permitted):
            return bool(self._permitted(principal, participant_id))
        return participant_id in (self._permitted.get(principal) or ())

    def _refusal_reason(self, principal: str) -> str:
        """Whether a refusal is a policy decision or a hole in the policy.

        Both refuse — D2 makes an uncovered principal fail closed, same as
        an explicitly refused one — so this changes no observable answer.
        It only lets an operator tell "I denied this" from "I forgot this",
        which are the same symptom with opposite fixes.

        The callable injection shape cannot express the difference (a
        predicate returns a bool and keeps its reasoning), so it reports
        the refusal without claiming to know which kind it was.
        """
        if callable(self._permitted):
            return "principal_not_permitted"
        if principal not in self._permitted:
            return "no_policy_for_principal"
        return "principal_not_permitted"

    def evidence(self, claim: IdentityClaim) -> IdentityBindingEvidence:
        """Full record of the binding decision for ``claim``."""
        participant_id = claim.participant_id
        principal = self.context.principal

        if principal is None:
            return IdentityBindingEvidence(
                binding=IdentityBinding.UNVERIFIED,
                participant_id=participant_id,
                principal=None,
                context_source=self.context.source,
                detail=(
                    "the legacy session carries no authenticated principal; "
                    "the claim is unchecked, not disproved"
                ),
                reason="no_principal_evidence",
            )

        if self._is_permitted(principal, participant_id):
            return IdentityBindingEvidence(
                binding=IdentityBinding.BOUND,
                participant_id=participant_id,
                principal=principal,
                context_source=self.context.source,
                detail=(
                    f"session principal {principal!r} (via {self.context.source}) "
                    f"may publish {participant_id!r} per the injected mapping"
                ),
            )

        reason = self._refusal_reason(principal)
        gap = reason == "no_policy_for_principal"
        return IdentityBindingEvidence(
            binding=IdentityBinding.UNBOUND,
            participant_id=participant_id,
            principal=principal,
            context_source=self.context.source,
            detail=(
                f"session principal {principal!r} (via {self.context.source}) is "
                + (
                    "not covered by the injected mapping"
                    if gap
                    else f"not permitted to publish {participant_id!r}"
                )
                + "; refusing"
            ),
            reason=reason,
        )

    def bind(self, claim: IdentityClaim) -> IdentityBinding:
        """The three-valued facet alone — the ``IdentityBinder`` port."""
        return self.evidence(claim).binding


def unverified_evidence(participant_id: str) -> IdentityBindingEvidence:
    """The evidence for a deployment that injected no binder at all.

    Explicit, so "nobody configured identity binding" and "a principal was
    checked and did not match" can never be read as the same state.
    """
    return IdentityBindingEvidence(
        binding=IdentityBinding.UNVERIFIED,
        participant_id=participant_id,
        principal=None,
        context_source=NO_CONTEXT,
        detail="no identity binder was injected; binding is unverified by construction",
        reason="no_binder_injected",
    )


__all__ = [
    "NO_CONTEXT",
    "IdentityBindingEvidence",
    "LegacySessionAuthContext",
    "LegacySessionIdentityBinder",
    "PermittedParticipants",
    "unverified_evidence",
]
