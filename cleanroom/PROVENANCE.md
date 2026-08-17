# Clean-room participant — provenance

**Claim.** `hb_cleanroom` is an independent MCP Heartbeat 0.1 participant. It was
authored from the normative artifacts only and shares no implementation logic
with the reference core.

## What it was built from

| Artifact | Role |
| --- | --- |
| `schema/mcp-heartbeat-0.1.schema.json` | Field names, types, patterns, `additionalProperties` / `patternProperties` rules, and the 3600s expiry ceiling |
| `docs/heartbeat-0.1.md` | The prose contract: what a heartbeat asserts and what it does not |
| `tests/fixtures/positive/`, `tests/fixtures/negative/` | Acceptance criteria — the corpus is the spec's executable half |

Nothing else. In particular, no module under `src/mcp_heartbeat/`,
`src/mcp_heartbeat_legacy/`, or `src/mcp_heartbeat_current/` was consulted for
implementation approach, and none is imported, linked, or vendored.

## How the claim is checked, not just stated

`mcp_heartbeat_conformance.cleanroom` verifies all four verbs in the hard
constraint mechanically, and its report is part of the HB-05 evidence pack:

| Verb | Check | Where |
| --- | --- | --- |
| **import** | AST walk of every clean-room module for any `mcp_heartbeat*` import | `no-imports` |
| **link** | Imported and exercised in a subprocess with `-S -s` and only `cleanroom/` on `PYTHONPATH` | `no-link` |
| **copy** | Longest *contiguous* run of shared normalised source lines, ceiling 4 | `no-copy` |
| **share logic** | Both implementations run against the same corpus, both interop directions, and the same lineage replays | `corpus-agreement`, `bidirectional-interop`, `lineage-agreement` |

### Why the copy check measures contiguity

Requiring zero shared lines does not work, and pretending it does would be
worse than not checking. Two implementations transcribing one schema both write
`for name in REQUIRED_FIELDS:`, because the contract names that field. What
separates copying from convergence is *contiguity*: a copied block is a run of
consecutive shared lines; independent transcription of shared vocabulary is
scattered singletons.

Import statements are excluded on principle rather than by allowlist. The
constraint forbids shared *implementation logic*; an import block declares which
stdlib modules a file needs, and Python has one spelling for each.

Measured at the time of writing: longest contiguous shared run **3 lines**,
scattered overlap ratio **0.127**, against a ceiling of 4 and 0.25.

## Where it is not installed

`cleanroom/` sits outside `src/` and is absent from `pyproject.toml`, so it is
never distributed with the package. An independent checker that shipped as part
of the thing it checks would not be independent.

## Known divergence

None at the time of writing: the two implementations agree on every fixture in
the corpus, in both interop directions, and on every lineage refusal reason
(`sequence_rollback`, `boot_id_reuse`, `sequence_conflict`, `duplicate`).

A future divergence is a finding about the **contract**, not about either
implementation — a specification two independent readers resolve differently is
under-specified, which is what an interoperability check exists to surface.
