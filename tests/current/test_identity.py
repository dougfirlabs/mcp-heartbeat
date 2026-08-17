"""Per-response identity binding — MCP-HB-03-S1, closing HB-00 defect D-05."""
from __future__ import annotations

import inspect

import pytest

from mcp_heartbeat.model import IdentityBinding, IdentityClaim

from mcp_heartbeat_current import identity as identity_module
from mcp_heartbeat_current.errors import IdentityUnbound
from mcp_heartbeat_current.identity import (
    Principal,
    RequestIdentityBinder,
    StaticPermittedParticipants,
    enforce,
)

PARTICIPANT = "svc/api-7"
OTHER = "svc/api-8"


@pytest.fixture()
def permitted(principal: Principal) -> StaticPermittedParticipants:
    return StaticPermittedParticipants({principal.compact(): {PARTICIPANT}})


# ── the three states ──────────────────────────────────────────────────


def test_a_permitted_principal_is_bound_and_authoritative(
    permitted: StaticPermittedParticipants, principal: Principal
) -> None:
    binding = RequestIdentityBinder(permitted, principal).evaluate(PARTICIPANT)
    assert binding.binding is IdentityBinding.BOUND
    assert binding.authoritative
    assert enforce(binding) is binding


def test_a_refused_principal_is_unbound_and_fails_closed(
    permitted: StaticPermittedParticipants, principal: Principal
) -> None:
    """Something authenticated and then claimed an identity it does not hold."""
    binding = RequestIdentityBinder(permitted, principal).evaluate(OTHER)
    assert binding.binding is IdentityBinding.UNBOUND
    assert not binding.authoritative
    with pytest.raises(IdentityUnbound) as excinfo:
        enforce(binding)
    assert excinfo.value.data["identity_binding"] == "unbound"


def test_absent_principal_evidence_stays_unverified_and_non_authoritative(
    permitted: StaticPermittedParticipants,
) -> None:
    """An unauthenticated read of a public lease is legitimate — just not proof."""
    binding = RequestIdentityBinder(permitted, None).evaluate(PARTICIPANT)
    assert binding.binding is IdentityBinding.UNVERIFIED
    assert binding.reason == "no_principal_evidence"
    assert not binding.authoritative
    assert enforce(binding) is binding, "unverified is not an error"


def test_an_empty_principal_is_not_a_principal(
    permitted: StaticPermittedParticipants,
) -> None:
    binding = RequestIdentityBinder(permitted, Principal()).evaluate(PARTICIPANT)
    assert binding.binding is IdentityBinding.UNVERIFIED


def test_silence_from_the_policy_is_not_consent(principal: Principal) -> None:
    """A deployment with no opinion fails closed, and never binds.

    Updated deliberately for operator decision **D2** (HB-X1). This test
    previously asserted ``unverified`` here — the legacy adapter answered
    ``unbound`` for the same input, and that observable divergence is the
    thing D2 settled. It is not weakened by the change: the assertion
    moved from the more permissive answer to the stricter one, and the
    reason code it always checked is unchanged.
    """

    class NoOpinion:
        def permits(self, principal: Principal, participant_id: str) -> bool | None:
            return None

    binding = RequestIdentityBinder(NoOpinion(), principal).evaluate(PARTICIPANT)
    assert binding.binding is IdentityBinding.UNBOUND
    assert binding.reason == "no_policy_for_principal"
    assert not binding.authoritative
    with pytest.raises(IdentityUnbound):
        enforce(binding)


def test_an_unlisted_principal_fails_closed_rather_than_degrading(
    permitted: StaticPermittedParticipants,
) -> None:
    """An incomplete map is a refusal, not an absence of evidence (D2).

    Something authenticated and *then* claimed a participant the policy
    does not cover it for; there is no absence here to be generous about.
    Renamed from ``..._degrades_to_unverified_not_unbound``, which named
    the pre-D2 answer in its title.
    """
    stranger = Principal(client_id="unknown", issuer="https://idp.example", subject="who")
    binding = RequestIdentityBinder(permitted, stranger).evaluate(PARTICIPANT)
    assert binding.binding is IdentityBinding.UNBOUND
    assert binding.reason == "no_policy_for_principal"
    assert not binding.authoritative


def test_unverified_now_means_exactly_one_thing(
    permitted: StaticPermittedParticipants, principal: Principal
) -> None:
    """After D2, ``unverified`` is reserved for "nobody authenticated".

    The value earned its keep by being narrow. Before D2 it covered two
    different situations — no principal, and a principal the policy did
    not mention — and the second one is why the eras could disagree. This
    pins the narrowing so it cannot quietly widen back.
    """
    absent = RequestIdentityBinder(permitted, None).evaluate(PARTICIPANT)
    empty = RequestIdentityBinder(permitted, Principal()).evaluate(PARTICIPANT)
    stranger = RequestIdentityBinder(
        permitted, Principal(client_id="nobody-listed")
    ).evaluate(PARTICIPANT)

    assert absent.binding is IdentityBinding.UNVERIFIED
    assert empty.binding is IdentityBinding.UNVERIFIED
    assert {absent.reason, empty.reason} == {"no_principal_evidence"}
    # An authenticated principal is never unverified, whatever the policy says.
    assert stranger.binding is not IdentityBinding.UNVERIFIED


def test_the_three_states_are_a_facet_not_a_ladder() -> None:
    """D-N2: three named states carrying no rung number.

    A ladder invites ``>= LEVEL_2``, and the first time someone writes that,
    ``unverified`` gets promoted. Names cannot be compared into a threshold,
    so the shape itself refuses the mistake.
    """
    import enum

    states = {IdentityBinding.BOUND, IdentityBinding.UNBOUND, IdentityBinding.UNVERIFIED}
    assert len(states) == 3
    assert not issubclass(IdentityBinding, enum.IntEnum)
    assert {s.value for s in states} == {"bound", "unbound", "unverified"}
    for state in states:
        assert not str(state.value).isdigit()
        assert not hasattr(state, "level") and not hasattr(state, "rank")


# ── the D-05 shape cannot be rebuilt ──────────────────────────────────


def test_binding_is_per_response_not_per_channel(
    permitted: StaticPermittedParticipants, principal: Principal
) -> None:
    """The defect, inverted.

    One authenticated gateway channel carries three participants. The
    legacy path reported one ``transport_authenticated`` for all of them;
    here each response is evaluated on its own and only the participant the
    principal actually holds comes back bound.
    """
    binder = RequestIdentityBinder(permitted, principal)
    verdicts = {p: binder.evaluate(p).binding for p in (PARTICIPANT, OTHER, "svc/api-9")}
    assert verdicts[PARTICIPANT] is IdentityBinding.BOUND
    assert verdicts[OTHER] is IdentityBinding.UNBOUND
    assert verdicts["svc/api-9"] is IdentityBinding.UNBOUND
    assert len(set(verdicts.values())) > 1, "a collapsed channel would agree on all three"


def test_the_binder_takes_its_principal_at_construction_and_offers_no_setter(
    permitted: StaticPermittedParticipants, principal: Principal
) -> None:
    """Structural, not procedural: there is no mutable "current principal"."""
    binder = RequestIdentityBinder(permitted, principal)
    setters = [
        name
        for name, _ in inspect.getmembers(binder, callable)
        if name.startswith("set_") or name in {"authenticate", "attach_principal"}
    ]
    assert setters == []
    assert isinstance(RequestIdentityBinder.principal, property)
    assert RequestIdentityBinder.principal.fset is None, "read-only by construction"
    with pytest.raises(AttributeError):
        binder.principal = Principal(client_id="someone-else")  # type: ignore[misc]


def test_no_channel_level_authentication_flag_exists_in_executable_code() -> None:
    """The legacy field name may be *explained*, never *used*.

    Checked over the AST rather than the raw text: a source-text search
    would be satisfied by deleting the docstring that records why D-05
    happened, which is the opposite of what this package should incentivise.
    """
    import ast

    tree = ast.parse(inspect.getsource(identity_module))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert "transport_authenticated" not in node.id
        elif isinstance(node, ast.Attribute):
            assert "transport_authenticated" not in node.attr
        elif isinstance(node, ast.arg):
            assert "transport_authenticated" not in node.arg
        elif isinstance(node, ast.Constant) and id(node) not in docstrings:
            assert "transport_authenticated" not in str(node.value)


def test_the_adapter_owns_no_policy_table(principal: Principal) -> None:
    """D-N3: the mapping is injected by a presence service or a deployment owner.

    The property under test is "an empty policy grants nothing", and it
    is unchanged. Only the *shape* of granting nothing moved, from
    ``unverified`` to ``unbound`` under D2 — so this asserts ``not
    BOUND``, which is what D-N3 actually claims, rather than re-pinning
    whichever refusal is currently in force.
    """
    empty = StaticPermittedParticipants({})
    binding = RequestIdentityBinder(empty, principal).evaluate(PARTICIPANT)
    assert binding.binding is not IdentityBinding.BOUND
    assert not binding.authoritative
    source = inspect.getsource(identity_module)
    assert "svc/" not in source, "no participant ids baked into the adapter"


# ── the core stays unable to elevate a claim ──────────────────────────


def test_the_binder_satisfies_the_core_port_without_the_core_importing_mcp(
    permitted: StaticPermittedParticipants, principal: Principal
) -> None:
    from mcp_heartbeat.ports import IdentityBinder as CorePort

    binder = RequestIdentityBinder(permitted, principal)
    assert isinstance(binder, CorePort)
    claim = IdentityClaim(participant_id=PARTICIPANT, epoch_id="epoch-a")
    assert binder.bind(claim) is IdentityBinding.BOUND
    assert claim.authenticated is False, "a self-claim is never proof"


# ── the facet travels beside the payload ──────────────────────────────


def test_the_facet_is_reported_separately_from_the_heartbeat(
    permitted: StaticPermittedParticipants, principal: Principal
) -> None:
    """Acceptance: recorded separately from the heartbeat payload."""
    from mcp_heartbeat.model import CORE_FIELDS

    facet = RequestIdentityBinder(permitted, principal).evaluate(PARTICIPANT).to_dict()
    assert facet["identity_binding"] == "bound"
    assert set(facet).isdisjoint(CORE_FIELDS)


def test_the_diagnostic_reason_is_never_a_seventh_wire_field(
    permitted: StaticPermittedParticipants,
) -> None:
    """The reason for a refusal travels beside the lease, never inside it.

    D2 gave a policy gap a reason worth reading, and a reason worth
    reading is exactly what gets helpfully attached to the document it
    describes. The heartbeat is six fields and its validity is decided by
    its own contents; a seventh member explaining why the transport
    refused a publisher would make the document's validity depend on the
    transport that carried it.
    """
    from mcp_heartbeat.clock import FakeClock
    from mcp_heartbeat.issuer import HeartbeatIssuer
    from mcp_heartbeat.model import CORE_FIELDS

    document = HeartbeatIssuer(
        participant_id=PARTICIPANT, epoch_id="epoch-a", clock=FakeClock()
    ).issue().to_dict()

    assert set(document) == set(CORE_FIELDS)
    assert len(document) == 6

    for refused in (
        RequestIdentityBinder(permitted, Principal(client_id="nobody-listed")).evaluate(
            PARTICIPANT
        ),
        RequestIdentityBinder(permitted, Principal(client_id="gw-1")).evaluate(OTHER),
    ):
        assert refused.reason, "a refusal must say why somewhere"
        assert "reason" in refused.to_dict()
        assert refused.reason not in document.values()
        assert set(refused.to_dict()).isdisjoint(CORE_FIELDS)


def test_the_fingerprint_is_stable_and_carries_no_credential(principal: Principal) -> None:
    assert principal.compact() == principal.compact()
    assert Principal.from_compact(principal.compact()) == principal
    assert "secret" not in principal.compact()
    assert Principal.from_compact(None) is None
    assert Principal.from_compact("not json") is None
