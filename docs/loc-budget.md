# Portable-core size budget

The HB-01 PRD sets a hard constraint:

> If the core exceeds 400 implementation LOC excluding schema/tests, stop and
> justify the expansion.

**The core measures 430 logical LOC and is therefore over budget by 30 (7.5%).
This document is that justification.**

> **Signed off — the overrun is ACCEPTED.** The operator accepted 430 on
> 2026-08-16, taking option 1 below. The machine-readable record is
> [`core-size-signoff.json`](core-size-signoff.json), and the release gate
> (`mcp_heartbeat_conformance.isolation`) reads *that* rather than this prose,
> so the decision is auditable instead of asserted.
>
> **The measurement did not change and was not permitted to.** The core still
> measures 430, the budget is still 400, and the overrun is still 30 — the
> counter, its inputs, and its exclusions are untouched. The signoff is keyed
> to that exact pair of numbers, so it accepts *this* measurement only: at 431,
> or against any budget other than 400, it lapses and the gate re-opens the
> question. Signing off an overrun and quietly raising a budget are different
> acts, and this is the one that leaves the budget where it was.

"Logical LOC" means lines of actual implementation: blank lines, comments, and
docstrings excluded. `tests/test_purity.py::logical_loc` is the measurement,
so the number in this document is machine-checked rather than asserted —
`test_an_overrun_of_the_prd_budget_is_documented` fails if this file stops
naming the current figure.

## Measurement

| Module | Logical LOC | Story |
| --- | ---: | --- |
| `model.py` | 126 | S1 |
| `validation.py` | 109 | S1 |
| `lineage.py` | 86 | S1 |
| `issuer.py` | 41 | S1 |
| `clock.py` | 34 | S1 |
| `errors.py` | 34 | S1 |
| **S1 portable core** | **430** | |
| `ports.py` | 48 | S2 |
| `__init__.py` | 15 | S2 |
| **Package total** | **493** | |

For context, the PRD's own story estimates are 200–400 LOC for S1 and 100–220
for S2, i.e. 300–620 for the package. The package total of 493 sits inside
that range; it is the narrower 400-LOC ceiling on the *core* that is exceeded.

## Why the 30 lines exist

Three costs are structural rather than incidental:

1. **The validator reports complete violation sets, not first-failure.**
   `validate_document` returns every violation so a conformance corpus can
   assert exact sets — the property that makes the corpus usable as a
   clean-room check. First-failure validation would be roughly 30 lines
   shorter and would make every negative fixture assert one arbitrary
   violation instead of the real set. This is the single largest contributor
   and it buys the thing the PRD asks the corpus to do.

2. **Specific reason codes for the expiry window.**
   `expiry_window_violation` exists so a rejection names
   `invalid_expiry_window` / `expiry_window_too_long` rather than a generic
   `schema_invalid`. Both codes are load-bearing in the threat model, and both
   were already frozen vocabulary in the HB-00 baseline.

3. **Issuance is in the core.** The PRD's goals name "boot, sequence,
   issuance, expiry, canonicalization" together, so `issuer.py` (41 LOC) is
   counted here. Treating issuance as an adapter concern would put the core at
   389 — under budget — but would leave the monotonic-counter and
   minted-epoch rules unowned by the contract that defines them, which is
   worse.

## What was done to stay close

- `__init__.py` composes `__all__` from each module's own rather than
  restating it: 87 logical LOC → 15, and the package surface can no longer
  drift from the modules'.
- Readiness, health, consistency, resource pressure, the verification ladder,
  attestation, evidence sinks, and the conformance runner all stayed out —
  together roughly 1,900 LOC of the legacy presence package.
- Two unused convenience properties (`Heartbeat.lease_seconds`,
  `LineageState.epoch_id`) were removed rather than shipped.

No further reduction was attempted, because the remaining options are
cosmetic — multiple statements per line, or collapsing `__all__` lists — which
would lower the number without shrinking the thing the budget is protecting.

## Options for the operator

1. **Accept 430** and lower `CORE_CEILING_LOC` if the core later shrinks.
2. **Move `issuer.py` out of the core** into an adapter-side helper: core
   drops to 389, under budget, at the cost described in (3) above.
3. **Relax the validator to first-failure**: roughly 30 LOC saved, at the cost
   of the exact-violation-set corpus contract.

Recommendation: option 1. The budget exists to stop the core from
re-accumulating the facets that made the legacy package hard to adopt, and by
that measure it is working — the excluded surface is 4× the overrun.

## The decision

Option 1, **accept 430**, taken on 2026-08-16 and recorded in
[`core-size-signoff.json`](core-size-signoff.json).

Options 2 and 3 were both available and both were declined for the reason they
are costed with above: each buys 30 lines by giving up something the PRD asked
for. Moving `issuer.py` out of the core would leave the monotonic-counter and
minted-epoch rules unowned by the contract that defines them; relaxing the
validator to first-failure would give up the exact-violation-set property the
conformance corpus is built on. Thirty lines is not worth either.

What the signoff does **not** authorize: raising `CORE_CEILING_LOC`, any
further growth of the core, or any publication step. It accepts one number.
