# HB-05 — cross-era conformance and release adjudication

**Status: STOPPED AT THE OPERATOR GATE.** Every release gate in the PRD is
`operator_approval_required`. This run verified, measured, and adjudicated. It
published nothing, uploaded nothing, pushed no image, and submitted nothing.

Regenerate everything below with:

```
python tools/emit_hb05_evidence.py --output docs/evidence/mcp-heartbeat-hb05
```

Exit code `0` means every matrix cleared; `1` means something is on HOLD or
FAILed and an operator has to look. This run exits `0`.

**Ready to publish is a statement about the evidence, not an event.** All three
levels are now green, and nothing was published, uploaded, pushed, or submitted
because of it. The gate block below is unchanged and stays that way: reaching
the top level is what hands the decision to an operator, not what makes it.

## The verdict

| Level | Ready? | Blocking |
| --- | :---: | --- |
| **Ready to dogfood** — the originating project may depend on this internally | ✅ | — |
| **Ready for external review** — the pack can go to an outside reviewer | ✅ | — |
| **Ready to publish** — the feature can go out | ✅ | — |

The three levels are computed **independently**, each from its own complete
criteria list (`mcp_heartbeat_conformance.release.LEVELS`). None is defined as
"the previous one plus something" — that is what the PRD's "none is inferred
from another" rules out, and `levels-independent` asserts it structurally.

## Matrix results

| Matrix | Pass | Fail | Hold | Unsupported |
| --- | ---: | ---: | ---: | ---: |
| cross-era | 7 | 0 | 0 | 0 |
| distributed-runtime | 10 | 0 | 0 | 0 |
| identity-binding | 10 | 0 | 0 | 0 |
| clean-room | 7 | 0 | 0 | 0 |
| measurement | 7 | 0 | 0 | 0 |
| isolation | 6 | 0 | 0 | 0 |
| release-gate | 4 | 0 | 0 | 0 |

**Nothing failed, and nothing is unproven.** `UNSUPPORTED` and `HOLD` are not
failures, and they are not passes either — that four-value vocabulary is what
lets this report be honest about the difference between "we proved it works",
"we proved it is broken", "we could not run it", and "it works but someone has
to decide something". The vocabulary is unchanged; what changed is that no case
needs the middle two any more.

## Residual risks — none open; here is how each closed

None of the three closed by being reclassified. The verdict vocabulary, the
seven publish criteria, and the thresholds each was measured against are all
exactly what they were — this section records what was *done* instead.

### 1. The official SDK v2 leg — CLOSED by running it (`cross-era/current-current-sdk`)

The pinned SDK (`mcp==2.0.0`, `mcp-types==2.0.0`, implementing `2026-07-28`)
cannot be installed here: it resolves `pydantic-core` to a version other than
the one a host application's `pydantic` pins, and mutating that environment for an
adapter that does not live in it is not a trade worth making.

**Closed 2026-08-17.** So the leg was run where it *can* be run.
`tools/verify_sdk.sh` builds a throwaway venv, installs the pin, and runs
`tests/current/` against the real SDK — **171 passed, 0 failed**, with all 18
pinned constants re-derived from the installed package and the 12-case
identifier grammar compared behaviourally. That run writes
[`sdk-verification.json`](sdk-verification.json), and the matrix reads it.

The record is **not** an assertion that the leg passed; it is a transcript of
the run that did, and it clears the case only while it still describes the tree
in front of it. It is keyed to a digest over the sources the run exercised —
`src/mcp_heartbeat`, `src/mcp_heartbeat_current`, `tests/current` — so editing
any line of them lapses the record and the case reverts to `UNSUPPORTED`. Same
non-transferability property the core-size signoff has against its `(430, 400)`
pair, and asserted directly by
`test_an_attestation_does_not_transfer_to_another_tree`.

**To re-prove after touching the adapter:** run `tools/verify_sdk.sh`, then
regenerate the pack. The evidence emitter still runs in the calling virtualenv, where
the SDK remains absent — that is deliberate and unchanged.

### 2. The portable core is over the PRD budget — CLOSED by decision D1

430 logical LOC against the PRD's 400 — 7.5% over, inside the recorded ceiling
of 440. The justification is in [`loc-budget.md`](loc-budget.md), which costed
three options (accept / move `issuer.py` out of the core / relax the validator
to first-failure).

**Closed 2026-08-16.** The operator accepted 430 (option 1). The signoff is a
machine-readable record, [`core-size-signoff.json`](core-size-signoff.json),
and `isolation/core-size` reads it rather than the prose — so the criterion is
satisfied by an auditable decision, not by an assertion in a document.

**The measurement did not change.** The core still measures 430, the budget is
still 400, the ceiling is still 440, and the counter, its inputs, and its
exclusions are untouched. The signoff is keyed to that exact pair of numbers,
so it accepts *this* measurement and nothing else: at 431, or against any other
budget, `signoff_covers` returns false and the case re-opens as a HOLD. Signing
off an overrun and quietly raising a budget are different acts, and
`test_a_signoff_does_not_transfer_to_a_different_measurement` is what keeps
them different.

### 3. The two eras answer a policy gap differently — CLOSED by decision D2

Found while building the identity matrix. For an authenticated principal with
**no entry** in the injected permitted-participants mapping, the two eras used
to answer differently:

| Era | Answer (before D2) | Rationale in the code |
| --- | --- | --- |
| current | `unverified` | "silence is not consent"; an incomplete map degrades to non-authoritative |
| legacy | `unbound` | an unlisted principal is refused, and `unbound` fails closed |

**Neither leaked authority** — both refused to be authoritative, and the safety
invariant (the eras never disagree about whether something is `bound`) held
across the whole 4×2 input sweep. But they were different *observable* answers,
and a consumer that alerts on `unbound` would page on one era and stay silent on
the other for an identical misconfiguration. That is what blocked publish.

**Closed 2026-08-16: fail-closed `unbound`, in both eras.** The operator ruled
that an authenticated principal absent from policy is a security event, not
missing evidence — there is no absence here, since something proved who it was
and *then* claimed a participant the policy does not cover it for. The current
adapter now answers `unbound` where it used to answer `unverified`; the legacy
adapter is unchanged, because it already did.

Two consequences worth stating:

* **`unverified` now means exactly one thing** — no principal authenticated at
  all. It used to cover two situations, and the second one is why the eras
  could disagree.
* **The diagnostic was not flattened with the answer.** Both eras still report
  *why* a refusal happened — `no_policy_for_principal` for a gap,
  `principal_not_permitted` for an explicit denial — because those are the same
  symptom with opposite fixes. The reason travels in the adjacent binding
  record, never as a seventh member of the six-field wire document; pinned by
  `identity-binding/wire-object-frozen`.

`identity-binding/era-agreement` now asserts the eras agree on **every** answer
across the input sweep, not merely on `bound`, which is the direct proof that
the divergence is gone rather than narrowed.

## A second finding, already closed in code

The two eras key their injected policy tables differently: the current adapter
by `Principal.compact()` (a JSON triple), the legacy adapter by the bare
principal string. Handing one dict to both looks to the modern path exactly
like a policy gap, because that is what it is.

Since D2 that fails closed rather than degrading to `unverified`, which makes
the misconfiguration loud instead of quiet: a deployment that gets the key
shape wrong stops binding entirely rather than silently reporting every request
as unauthenticated. Louder is the right direction, but it is still a
misconfiguration and not an attack, and the `no_policy_for_principal` reason
code says so.

The binding *semantics* agree; the *injection contract* does not. A
presence service owns the mapping and must build both shapes. Pinned by
`identity-binding/policy-key-shape` so a future change to either key shape fails
a case instead of taking a deployment's bindings with it.

## Measured overhead

| Measure | Value | Threshold | |
| --- | ---: | ---: | :---: |
| Bytes per heartbeat (minimal, canonical) | 161 | 512 | ✅ |
| Bytes per heartbeat (with extensions) | 241 | 2048 | ✅ |
| CPU, 10 000 issue+admit cycles | ~0.13 s | 10 s | ✅ |
| Retained heap per beat | ~0.9 B | 64 B | ✅ |
| Emission, 30 s lease at 0.5 renew | 240 beats/h (~39 KB/h) | cadence × 1.25 | ✅ |
| Fetches per 5 000-hint burst | 1 | 1 | ✅ |

Bytes and fetch counts are exact and machine-independent. CPU and memory come
from the run that produced the pack; their thresholds are set where crossing
them means an *algorithmic* change, not a slow afternoon.

Abuse resistance: a 5 000-hint flood costs one authoritative read
(amplification 0.0002), and a 1 000-document malformed flood admits nothing,
raises nothing, accumulates nothing, and still admits a good document
afterwards.

## The independent participant

`cleanroom/hb_cleanroom/` is a second MCP Heartbeat 0.1 implementation written
from the schema, prose, and corpus only. All four verbs in the hard constraint
are checked mechanically rather than asserted — see
[`../cleanroom/PROVENANCE.md`](../cleanroom/PROVENANCE.md).

Longest contiguous shared source run: **3 lines** (ceiling 4). Scattered overlap
ratio **0.127** (ceiling 0.25). The two implementations agree on every corpus
fixture, in both interop directions, and on every lineage refusal reason.

## What was not done, deliberately

Per the PRD's non-goals and release gates: the feature is not published, no MCP
SEP or AAIF proposal was submitted, no package was uploaded, no image was
pushed, no durable general event system was built, and production dispatch from
presence remains inactive — `policy.authorize_from_heartbeat` still refuses
unconditionally, re-checked from the consuming project's side.
