# The current-era adapter (MCP 2026-07-28)

`mcp_heartbeat_current` binds the portable heartbeat core to MCP revision
`2026-07-28`. It is greenfield: no legacy module was renamed, reused, or
repurposed to produce it.

---

## What the modern revision changed

| Legacy (2025-11-25 and earlier) | Current (2026-07-28) |
|---|---|
| `initialize` / `notifications/initialized` handshake | `server/discover` — one request, no lifecycle |
| Version negotiated once, remembered in a session | Per-request `_meta` + three mirrored headers |
| `Mcp-Session-Id` header | *removed* — ignored on receipt, never minted or echoed |
| `resources/subscribe` + standalone `GET` SSE stream | `subscriptions/listen`, notifications on its own response stream |
| `capabilities.experimental.<key>` | `capabilities.extensions.<prefixed-id>` |

Everything in the right-hand column is required; everything in the left is
forbidden on this path. `lint.py` proves the absence mechanically.

## Layout

| Module | Responsibility |
|---|---|
| `contract.py` | Every pinned literal: revisions, `_meta` keys, headers, error codes, the extension id, the SDK pin, the forbidden table. Nothing else may spell them. |
| `metadata.py` | The per-request envelope, and the three-rung inbound ladder. |
| `discovery.py` | `server/discover` and extension negotiation on two independent axes. |
| `identity.py` | The `identity_binding` facet, computed **per response**. |
| `subscriptions.py` | `subscriptions/listen`, ack-first ordering, hint translation. |
| `convergence.py` | Authoritative refetch, reconciliation, deployment-topology verdicts. |
| `era.py` | Explicit era routing; refuses anything confusable between eras. |
| `lint.py` | AST scan for forbidden primitives and stray SDK imports. |
| `sdk.py` | **The only module that imports the official SDK.** |
| `errors.py` | Adapter failures, each carrying a JSON-RPC code. |

## Two rules that shape everything

**A hint decides nothing.** The only legal effect of a change notification
is to schedule a refetch. The refetch deadline is derived from the *held
lease's own expiry*, so a consumer that receives no hint at all still
converges — it is late, never wrong. This is why lost, duplicated,
reordered and forged hints are all latency questions rather than
correctness questions, and why the fault matrix is boring.

**Identity is bound per response, never per channel.** HB-00 defect D-05
was a single `transport_authenticated` flag per channel: behind a gateway,
one authenticated tenant got "authenticated" for every participant sharing
its connection. `RequestIdentityBinder` takes its principal at construction
and exposes no setter, so there is no mutable "current principal" for a
gateway to collapse onto.

## Using it

```python
from mcp_heartbeat.clock import SystemClock
from mcp_heartbeat_current.convergence import HeartbeatConsumer
from mcp_heartbeat_current.discovery import negotiate
from mcp_heartbeat_current.identity import RequestIdentityBinder

negotiation = negotiate(discover_result)          # both axes, separately
if not negotiation.heartbeat_enabled:
    ...                                            # MCP is still fine; heartbeat is off

consumer = HeartbeatConsumer(
    "svc/api-7",
    source,                                        # your resources/read binding
    SystemClock(),
    binder_factory=lambda p: RequestIdentityBinder(policy, p),
)

consumer.on_hint(hint)                             # optional; schedules a refetch
verdict = consumer.poll()                          # refetches iff one is owed
```

`policy` is yours — an object with `permits(principal, participant_id) -> bool | None`.
Per D-N3 the adapter owns no policy table; a presence service or another
deployment owner supplies the mapping.

`None` means "no opinion". For an **authenticated** principal it yields
`unbound` — fail closed — per operator decision D2 (HB-X1). Silence is never
consent, and it is not an absence of evidence either: something proved who it
was and then claimed a participant your policy does not cover it for. That is
`heartbeat-0.1.md` §5's `unbound` exactly ("a principal was determined and is
**not** permitted"), and it is the same answer the legacy adapter has always
given, so both eras agree.

`unverified` is reserved for the one case where evidence is genuinely absent:
no principal at all. The seam keeps all three answers — `permits` still returns
`None` rather than `False` for an uncovered principal — because the diagnostic
reason then distinguishes `no_policy_for_principal` from
`principal_not_permitted`. Same refusal, different fix.

## Deployment topologies

`convergence.TOPOLOGY_RULES` pins a deterministic verdict for each shape the
PRD names, and `tests/current/test_delivery_faults.py` drives each one to it.

| Topology | Verdict |
|---|---|
| Round-robin replicas under one participant id | `refused` / `boot_id_reuse` — the split is surfaced, not averaged |
| Gateway termination | `advanced`, bound per response |
| Serverless cold start | `advanced` — a new epoch is a new stream, not a rollback |
| Rolling deployment, old pod still publishing | `refused` / `boot_id_reuse` |
| Asymmetric connectivity (hints up, reads down) | `unreachable` — nothing fabricated, lease expires on schedule |
| Backpressure | `advanced` — N hints coalesce to one refetch |

## The official SDK

Pinned exactly: `mcp==2.0.0`, `mcp-types==2.0.0`, installed via the
`current` extra. `sdk.assert_contract_matches_sdk()` re-derives all 18
constants from the installed SDK and compares behaviour on the extension-id
grammar, so the pure layer is a *checked* copy rather than a decaying one.

The SDK is **not** installed in a host application's shared virtualenv: doing so resolves
`pydantic-core` away from the version the installed `pydantic` pins. The
conformance lane therefore runs in its own environment:

```bash
tools/verify_sdk.sh            # run the lane
tools/verify_sdk.sh --evidence # and regenerate evidence
```

Without the SDK, `tests/current/` skips exactly the 10 conformance tests and
everything else runs. `test_the_skip_is_only_ever_about_a_missing_sdk` runs
in both environments and asserts the suite can skip for that one reason and
no other.

## Evidence

`docs/evidence/mcp-heartbeat-hb03/` — SDK provenance, a full protocol
transcript, the forbidden-primitive lint, the subscription fault matrix, and
the era-matrix supersession record.
