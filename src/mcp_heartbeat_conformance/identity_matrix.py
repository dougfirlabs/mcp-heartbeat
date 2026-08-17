"""Identity binding, asserted identically across both eras.

The normative rule is short and easy to get wrong: a self-declared
``node_id`` is a *claim*, not authenticated identity. Adapters therefore
report a separate three-valued facet — ``bound``, ``unbound``,
``unverified`` — and the three values are not interchangeable:

``bound``
    The request's authenticated principal is permitted to publish this
    participant. Only this value may move authoritative state.
``unbound``
    Something authenticated and then claimed a participant it does not
    hold. This is evidence of a problem, so it fails closed.
``unverified``
    **No principal evidence at all.** An absence of evidence is not
    evidence of a problem — it is legitimate, readable, and never
    authoritative.

Note the boundary between the last two, which operator decision **D2**
(HB-X1) moved. "No policy covering the principal" used to land in
``unverified`` on the current-era path and in ``unbound`` on the legacy
one. It is now ``unbound`` on both: a principal that authenticated and
then claimed a participant the policy does not cover it for is not an
absence of evidence, and the two eras must not answer the same question
two different ways. ``unverified`` now means exactly one thing — nobody
authenticated. See :func:`a_policy_gap_is_answered_the_same_way_by_both_eras`.

The eras implement this with different machinery (a per-request
principal on the modern path, a session context on the legacy path), so
the risk is that they drift into different *answers*. Every case here
therefore asks both eras the same question and asserts they agree, and
:func:`eras_agree_on_every_input` sweeps the full input space to prove
the agreement is not a coincidence of three hand-picked examples.
"""
from __future__ import annotations

from typing import Any

from mcp_heartbeat.clock import FakeClock
from mcp_heartbeat.issuer import HeartbeatIssuer
from mcp_heartbeat.model import CORE_FIELDS, IdentityBinding, IdentityClaim
from mcp_heartbeat_current.convergence import Convergence, FetchResult, HeartbeatConsumer
from mcp_heartbeat_current.errors import IdentityUnbound
from mcp_heartbeat_current.identity import (
    Principal,
    RequestIdentityBinder,
    StaticPermittedParticipants,
    enforce,
)
from mcp_heartbeat_legacy.identity import (
    LegacySessionAuthContext,
    LegacySessionIdentityBinder,
    unverified_evidence,
)

from .verdicts import Case, MatrixReport, run_cases

PARTICIPANT = "svc/api-7"
OTHER_PARTICIPANT = "svc/billing-2"
PRINCIPAL_ID = "spiffe://lab/api-7"
EPOCH = "epoch-a"

#: The principal-to-participant mapping, as one source of truth. Neither
#: adapter owns a policy table — a presence service injects this — but
#: the two eras key theirs differently, so both shapes are derived from
#: this one dict rather than written twice:
#:
#: * the current adapter keys by :meth:`Principal.compact`, a JSON triple,
#:   because a modern principal is ``(client_id, issuer, subject)``;
#: * the legacy adapter keys by the bare session principal string,
#:   because a legacy session has exactly one.
#:
#: That difference is an injection-contract asymmetry, not a semantic
#: one, and :func:`eras_agree_on_every_input` is what holds it to that.
PERMITTED = {PRINCIPAL_ID: frozenset({PARTICIPANT})}

#: The same policy in the current era's key shape.
CURRENT_PERMITTED = {
    Principal(client_id=principal).compact(): participants
    for principal, participants in PERMITTED.items()
}


def _current_binding(principal: Principal | None, participant_id: str = PARTICIPANT):
    binder = RequestIdentityBinder(
        StaticPermittedParticipants(CURRENT_PERMITTED), principal
    )
    return binder.evaluate(participant_id)


def _current_binder(principal: Principal | None) -> RequestIdentityBinder:
    return RequestIdentityBinder(StaticPermittedParticipants(CURRENT_PERMITTED), principal)


def _legacy_binding(principal: str | None, participant_id: str = PARTICIPANT):
    binder = LegacySessionIdentityBinder(
        context=LegacySessionAuthContext(
            principal=principal, source="session" if principal else "none"
        ),
        permitted=PERMITTED,
    )
    return binder.evidence(IdentityClaim(participant_id=participant_id, epoch_id=EPOCH))


# ── the cases ─────────────────────────────────────────────────────────


def bound_identity(case: Case) -> None:
    """A permitted principal binds, on both eras, and may be authoritative."""
    current = _current_binding(Principal(client_id=PRINCIPAL_ID))
    legacy = _legacy_binding(PRINCIPAL_ID)

    case.check("current_era_binds", current.binding is IdentityBinding.BOUND, current)
    case.check("legacy_era_binds", legacy.binding is IdentityBinding.BOUND, legacy)
    case.check("the_eras_agree", current.binding is legacy.binding)
    case.check("enforce_admits_a_bound_binding", enforce(current) is current)
    case.check(
        "the_principal_is_recorded_as_a_fingerprint_not_verbatim",
        current.principal_fingerprint is not None
        and current.principal_fingerprint != PRINCIPAL_ID,
        current.principal_fingerprint,
    )
    case.observations = {"current": current, "legacy": legacy}


def unbound_identity_fails_closed(case: Case) -> None:
    """An authenticated principal claiming another participant is refused."""
    current = _current_binding(Principal(client_id=PRINCIPAL_ID), OTHER_PARTICIPANT)
    legacy = _legacy_binding(PRINCIPAL_ID, OTHER_PARTICIPANT)

    case.check("current_era_reports_unbound", current.binding is IdentityBinding.UNBOUND, current)
    case.check("legacy_era_reports_unbound", legacy.binding is IdentityBinding.UNBOUND, legacy)
    case.check("the_eras_agree", current.binding is legacy.binding)

    raised = None
    try:
        enforce(current)
    except IdentityUnbound as exc:
        raised = str(exc)
    case.check("enforce_fails_closed_on_unbound", raised is not None, raised)
    case.check(
        "the_refusal_names_the_participant_not_the_principal",
        raised is not None and OTHER_PARTICIPANT in raised and PRINCIPAL_ID not in raised,
        raised,
    )
    case.observations = {"current": current, "legacy": legacy, "raised": raised}


def unverified_is_not_a_failure(case: Case) -> None:
    """No principal evidence stays readable, and never becomes authoritative.

    Both halves matter. Treating an unauthenticated read as a failure
    would break every legitimate public consumer; treating it as bound
    would present a guess as a fact.
    """
    current = _current_binding(None)
    legacy = _legacy_binding(None)

    case.check("current_era_reports_unverified", current.binding is IdentityBinding.UNVERIFIED, current)
    case.check("legacy_era_reports_unverified", legacy.binding is IdentityBinding.UNVERIFIED, legacy)
    case.check("the_eras_agree", current.binding is legacy.binding)
    case.check("unverified_does_not_raise", enforce(current) is current)
    case.check(
        "the_reason_says_evidence_is_absent_not_wrong",
        current.reason == "no_principal_evidence",
        current.reason,
    )
    case.check(
        "the_legacy_helper_agrees_with_the_binder",
        unverified_evidence(PARTICIPANT).binding is IdentityBinding.UNVERIFIED,
    )
    case.observations = {"current": current, "legacy": legacy}


def an_empty_principal_is_not_a_principal(case: Case) -> None:
    """A principal object with nothing in it must not count as evidence.

    The spoof this closes is a caller that constructs ``Principal()`` to
    satisfy a type and accidentally converts "unauthenticated" into
    "authenticated as nobody".
    """
    empty = _current_binding(Principal())
    case.check("empty_principal_is_unverified", empty.binding is IdentityBinding.UNVERIFIED, empty)
    case.check("not_bound", empty.binding is not IdentityBinding.BOUND)
    case.check("reason_is_absent_evidence", empty.reason == "no_principal_evidence", empty.reason)
    case.observations = {"binding": empty}


def an_unknown_principal_is_not_bound(case: Case) -> None:
    """Silence from the policy is not consent.

    A principal the deployment has no mapping for must not fall through
    to permitted. Since operator decision D2 it falls to ``unbound`` on
    both eras — see ``policy-gap-divergence`` — but the property this
    case pins is the weaker and more important one: *never* ``bound``.
    Stated independently of which refusal is chosen, so a future change
    to the refusal semantics cannot silently take this guarantee with it.
    """
    stranger = _current_binding(Principal(client_id="spiffe://lab/unknown"))
    legacy = _legacy_binding("spiffe://lab/unknown")

    case.check("current_era_is_not_bound", stranger.binding is not IdentityBinding.BOUND, stranger)
    case.check("legacy_era_is_not_bound", legacy.binding is not IdentityBinding.BOUND, legacy)
    case.check("never_silently_permitted", stranger.binding is not IdentityBinding.BOUND)
    case.observations = {"current": stranger, "legacy": legacy}


def unbound_never_moves_authoritative_state(case: Case) -> None:
    """The end-to-end claim: identity gates convergence, not just reporting.

    A facet that is computed and then ignored would satisfy every case
    above and still be worthless. This drives the real consumer and
    asserts held state does not move.
    """
    clock = FakeClock()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id=EPOCH, clock=clock)
    documents = {PARTICIPANT: issuer.issue().to_dict()}
    principal_box: list[Principal | None] = [Principal(client_id=PRINCIPAL_ID)]

    class Source:
        def fetch(self, participant_id: str) -> FetchResult:
            return FetchResult(document=documents[participant_id], principal=principal_box[0])

    consumer = HeartbeatConsumer(
        PARTICIPANT,
        Source(),
        clock,
        binder_factory=_current_binder,
    )
    case.check("a_bound_read_converges", consumer.refetch().convergence is Convergence.ADVANCED)
    held = consumer.held

    # The principal changes to one that may not publish this participant.
    principal_box[0] = Principal(client_id="spiffe://lab/impostor")
    clock.advance(1.0)
    documents[PARTICIPANT] = issuer.issue().to_dict()
    verdict = consumer.refetch()

    case.check(
        "an_unbound_read_is_refused",
        verdict.convergence in (Convergence.IDENTITY_UNBOUND, Convergence.NON_AUTHORITATIVE),
        verdict,
    )
    case.check(
        "held_state_did_not_move",
        consumer.held is not None and held is not None and consumer.held.revision == held.revision,
        {"before": held.revision if held else None,
         "after": consumer.held.revision if consumer.held else None},
    )

    # No principal at all: readable, still not authoritative.
    principal_box[0] = None
    clock.advance(1.0)
    documents[PARTICIPANT] = issuer.issue().to_dict()
    unverified = consumer.refetch()
    case.check(
        "an_unverified_read_is_non_authoritative",
        unverified.convergence is Convergence.NON_AUTHORITATIVE,
        unverified,
    )
    case.check(
        "and_still_did_not_move_held_state",
        consumer.held is not None and held is not None and consumer.held.revision == held.revision,
    )
    case.observations = {"unbound": verdict, "unverified": unverified}


def eras_agree_on_every_input(case: Case) -> None:
    """Sweep the whole input space rather than three examples.

    Four principals × two participants is small enough to enumerate
    exhaustively, which turns "the eras agree" from an anecdote into a
    property.
    """
    principals = (None, "", PRINCIPAL_ID, "spiffe://lab/unknown")
    participants = (PARTICIPANT, OTHER_PARTICIPANT)
    disagreements: list[dict[str, Any]] = []
    table: list[dict[str, Any]] = []

    for raw in principals:
        for participant in participants:
            current = _current_binding(
                Principal(client_id=raw) if raw is not None else None, participant
            )
            legacy = _legacy_binding(raw or None, participant)
            row = {
                "principal": raw,
                "participant": participant,
                "current": current.binding.value,
                "legacy": legacy.binding.value,
            }
            table.append(row)
            if current.binding is not legacy.binding:
                disagreements.append(row)

    # The safety-critical invariant is agreement on `bound`, because
    # `bound` is the only value that may move authoritative state.
    bound_disagreements = [
        row for row in disagreements if "bound" in (row["current"], row["legacy"])
    ]
    case.check("every_combination_was_evaluated", len(table) == 8, len(table))
    case.check(
        "the_eras_never_disagree_about_whether_something_is_bound",
        bound_disagreements == [],
        bound_disagreements,
    )
    # Since operator decision D2 the eras agree on *every* answer, not
    # just on `bound`. This used to be the weaker claim above plus one
    # tolerated divergence (`policy-gap-divergence`); the sweep is now
    # the direct proof that the divergence is gone, across the whole
    # input space rather than at the single point D2 was about.
    case.check(
        "and_since_D2_they_agree_on_every_answer_not_just_on_bound",
        disagreements == [],
        disagreements,
    )
    case.check(
        "bound_only_ever_appears_for_the_permitted_pair",
        all(
            row["current"] != "bound"
            or (row["principal"] == PRINCIPAL_ID and row["participant"] == PARTICIPANT)
            for row in table
        ),
        [row for row in table if row["current"] == "bound"],
    )
    case.check(
        "no_era_ever_grants_authority_without_a_policy_hit",
        all(row["legacy"] != "bound" or row["principal"] == PRINCIPAL_ID for row in table),
        [row for row in table if row["legacy"] == "bound"],
    )
    case.observations = {"table": table, "non_bound_disagreements": disagreements}


def a_policy_gap_is_answered_the_same_way_by_both_eras(case: Case) -> None:
    """Operator decision D2: a policy gap is fail-closed ``unbound``, in both eras.

    Found by :func:`eras_agree_on_every_input` and held open through
    HB-05. For an authenticated principal with **no entry** in the
    injected mapping, the two eras used to answer differently:

    * the current adapter answered ``unverified`` — "silence is not
      consent", an incomplete map degrades to non-authoritative;
    * the legacy adapter answered ``unbound`` — an unlisted principal is
      refused, and ``unbound`` fails closed.

    Neither leaked authority, so it was never a defect. But they were
    different *observable* answers to the same question, and a consumer
    that alerts on ``unbound`` would page on one era and stay silent on
    the other for an identical misconfiguration. HB-05 recorded that as a
    HOLD rather than picking a winner, because choosing between "a policy
    gap is a security event" and "a policy gap is missing evidence" is a
    threat-model decision an operator makes, not a verification PRD.

    **The operator chose fail-closed.** An authenticated principal is not
    an absence of evidence: something proved who it was and then claimed
    a participant the policy does not cover it for. So both eras now
    answer ``unbound``, and ``unverified`` is reserved for the one case
    where evidence is genuinely absent — no principal at all.

    This case asserts the *observable* answers are identical. It
    deliberately does not require the diagnostics to be identical: the
    reason code, which says whether the refusal was a policy decision or
    a hole in the policy, is adjacent diagnostic data and each era spells
    its record its own way.
    """
    stranger = "spiffe://lab/unknown"
    current = _current_binding(Principal(client_id=stranger))
    legacy = _legacy_binding(stranger)

    case.check("neither_era_grants_bound", IdentityBinding.BOUND not in (current.binding, legacy.binding))
    case.check("current_era_answers_unbound", current.binding is IdentityBinding.UNBOUND, current)
    case.check("legacy_era_answers_unbound", legacy.binding is IdentityBinding.UNBOUND, legacy)
    case.check(
        "the_two_eras_give_the_identical_observable_answer",
        current.binding is legacy.binding,
        {"current": current.binding.value, "legacy": legacy.binding.value},
    )
    case.check("so_neither_can_move_authoritative_state", not current.authoritative)

    # Fail-closed has to mean something enforceable, not just a label.
    raised = None
    try:
        enforce(current)
    except IdentityUnbound as exc:
        raised = str(exc)
    case.check("and_the_current_era_actually_fails_closed", raised is not None, raised)

    # The diagnostic survives the normalization: both eras still say
    # *which* kind of refusal this was, without that showing up in the
    # observable answer.
    case.check(
        "both_eras_still_report_why_it_was_refused",
        current.reason == "no_policy_for_principal"
        and legacy.reason == "no_policy_for_principal",
        {"current": current.reason, "legacy": legacy.reason},
    )
    case.check(
        "and_that_reason_is_distinct_from_an_explicit_refusal",
        _current_binding(Principal(client_id=PRINCIPAL_ID), OTHER_PARTICIPANT).reason
        == "principal_not_permitted",
    )

    case.observations = {
        "current": current,
        "legacy": legacy,
        "decision": (
            "operator decision D2 (HB-X1): an authenticated principal absent from "
            "the injected mapping is a security event, not missing evidence. Both "
            "eras answer unbound and fail closed; the reason code that distinguishes "
            "a policy gap from an explicit refusal travels beside the answer."
        ),
    }


def the_policy_key_shapes_differ_by_era(case: Case) -> None:
    """The injection contract is asymmetric, and that must be explicit.

    Discovered while building this matrix: the current adapter keys its
    permitted-participants mapping by ``Principal.compact()`` and the
    legacy adapter keys its by the bare principal string. Handing one
    dict to both looks to the modern path exactly like a policy gap —
    "no policy for principal" — because that is what it is.

    Under operator decision D2 that now fails closed rather than
    degrading to ``unverified``, which makes the misconfiguration loud
    instead of quiet: a deployment that gets the key shape wrong stops
    binding entirely rather than silently reporting every request as
    unauthenticated. Louder is the right direction here, but it is still
    a misconfiguration and not an attack, and the reason code says so.

    The consumer of this asymmetry is a presence service, which owns the
    mapping. Pinning it here means a future change to either key shape
    fails this case instead of taking a deployment's bindings with it.
    """
    shared = {PRINCIPAL_ID: frozenset({PARTICIPANT})}

    naive = RequestIdentityBinder(
        StaticPermittedParticipants(shared), Principal(client_id=PRINCIPAL_ID)
    ).evaluate(PARTICIPANT)
    case.check(
        "the_bare_string_key_does_not_bind_on_the_current_era",
        naive.binding is IdentityBinding.UNBOUND,
        naive,
    )
    case.check(
        "and_fails_closed_rather_than_falling_through_to_permitted",
        naive.binding is not IdentityBinding.BOUND and naive.reason == "no_policy_for_principal",
        naive.reason,
    )

    correct = _current_binding(Principal(client_id=PRINCIPAL_ID))
    case.check("the_compact_key_binds", correct.binding is IdentityBinding.BOUND, correct)

    legacy = _legacy_binding(PRINCIPAL_ID)
    case.check("the_bare_string_key_binds_on_the_legacy_era", legacy.binding is IdentityBinding.BOUND)
    case.check(
        "so_the_two_key_shapes_are_genuinely_different",
        Principal(client_id=PRINCIPAL_ID).compact() != PRINCIPAL_ID,
        {"current_key": Principal(client_id=PRINCIPAL_ID).compact(), "legacy_key": PRINCIPAL_ID},
    )
    case.observations = {
        "current_key_shape": Principal(client_id=PRINCIPAL_ID).compact(),
        "legacy_key_shape": PRINCIPAL_ID,
        "consumer": "A presence service owns and injects both mappings",
    }


def the_diagnostic_reason_never_widens_the_wire_object(case: Case) -> None:
    """The reason for a refusal travels *beside* the lease, never inside it.

    D2 gave a policy gap a diagnostic reason worth reading, and a reason
    worth reading is exactly the kind of thing that gets helpfully
    attached to the document it describes. It must not be. The heartbeat
    is six fields, and its validity is decided by its own contents; a
    seventh member carrying "why the transport refused this publisher"
    would make the document's validity depend on the transport that
    carried it, and would fork the wire format on a diagnostic.

    So this case pins the field set of the thing that crosses the wire
    and asserts the reason is reachable only from the adjacent binding
    record — which is where every consumer already reads it from.
    """
    clock = FakeClock()
    issuer = HeartbeatIssuer(participant_id=PARTICIPANT, epoch_id=EPOCH, clock=clock)
    document = issuer.issue().to_dict()

    case.check(
        "the_wire_object_is_exactly_the_six_declared_fields",
        set(document) == set(CORE_FIELDS),
        {"observed": sorted(document), "declared": sorted(CORE_FIELDS)},
    )
    case.check("and_there_are_six_of_them", len(document) == 6, len(document))

    # The two refusals that carry a reason, on both eras. None of them
    # may put it in the document.
    refusals = {
        "current_policy_gap": _current_binding(Principal(client_id="spiffe://lab/unknown")),
        "current_explicit": _current_binding(Principal(client_id=PRINCIPAL_ID), OTHER_PARTICIPANT),
        "legacy_policy_gap": _legacy_binding("spiffe://lab/unknown"),
        "legacy_explicit": _legacy_binding(PRINCIPAL_ID, OTHER_PARTICIPANT),
    }
    case.check(
        "every_refusal_carries_a_reason_in_its_own_record",
        all(refusal.reason for refusal in refusals.values()),
        {name: refusal.reason for name, refusal in refusals.items()},
    )
    case.check(
        "and_none_of_those_reasons_reached_the_wire_object",
        set(issuer.issue().to_dict()) == set(CORE_FIELDS),
        sorted(issuer.issue().to_dict()),
    )
    case.check(
        "no_binding_or_diagnostic_vocabulary_is_a_wire_member",
        not ({"reason", "identity_binding", "principal", "principal_fingerprint",
              "authoritative", "detail", "context_source"} & set(document)),
        sorted(set(document)),
    )
    case.check(
        "the_reason_is_reachable_from_the_adjacent_record_instead",
        all("reason" in refusal.to_dict() for refusal in refusals.values()),
        {name: sorted(refusal.to_dict()) for name, refusal in refusals.items()},
    )
    case.observations = {
        "wire_fields": sorted(CORE_FIELDS),
        "wire_field_count": len(document),
        "reasons_by_era": {name: refusal.reason for name, refusal in refusals.items()},
    }


CASES: tuple[tuple[str, str, Any], ...] = (
    ("bound", "A permitted principal binds on both eras", bound_identity),
    ("unbound", "A mismatched principal fails closed on both eras", unbound_identity_fails_closed),
    ("unverified", "No principal evidence stays readable and non-authoritative", unverified_is_not_a_failure),
    ("empty-principal", "An empty principal is not evidence", an_empty_principal_is_not_a_principal),
    ("unknown-principal", "Policy silence is not consent", an_unknown_principal_is_not_bound),
    ("gates-convergence", "Identity gates authoritative state, not just reporting", unbound_never_moves_authoritative_state),
    ("era-agreement", "Both eras agree across the whole input space", eras_agree_on_every_input),
    ("policy-key-shape", "The injected policy key shape differs by era, explicitly", the_policy_key_shapes_differ_by_era),
    # Keeps its original case id. The id is what the release gate's
    # `identity_semantics_settled` criterion and every prior evidence
    # pack refer to, so renaming it on the day it closes would break the
    # trail between "this was open" and "this is how it closed".
    ("policy-gap-divergence", "A policy gap is answered the same way by both eras (D2)", a_policy_gap_is_answered_the_same_way_by_both_eras),
    ("wire-object-frozen", "The diagnostic reason never widens the six-field wire object", the_diagnostic_reason_never_widens_the_wire_object),
)


def run() -> MatrixReport:
    """Run the identity-binding matrix. Always completes."""
    report = MatrixReport(
        matrix_id="identity-binding",
        title="Authenticated identity binding and spoof/mismatch negatives across both eras",
    )
    return run_cases(report, CASES)


__all__ = ["CASES", "PARTICIPANT", "PERMITTED", "PRINCIPAL_ID", "run"]
