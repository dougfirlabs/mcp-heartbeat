# Legacy MCP compatibility statement

**Adapter:** `mcp_heartbeat_legacy` 0.1.0
**Heartbeat extension version:** 0.1
**MCP protocol era:** `2025-06-18` (also negotiates `2025-03-26`)
**Status:** supported for compatibility. **Not normative for new work.**

This document is the compatibility and deprecation policy required by
`mcp-heartbeat-02-legacy-adapter-repair` story MCP-HB-02-S2. It describes what
the legacy adapter promises, what it deliberately does not, and how the promise
ends.

---

## 1. Supported protocol revisions

| revision | negotiated | notes |
| --- | --- | --- |
| `2025-06-18` | yes | the revision the originating integration lab pins; the adapter's default |
| `2025-03-26` | yes | accepted when a client asks for it explicitly |
| `2026-07-28` | **no** | refused with reason `modern_era_refused`; served by the current adapter (HB-03) |
| anything else | **no** | refused with reason `unsupported_version` |

The supported set is closed. A revision that is not in the table is refused,
never echoed — see §3, defect D-04.

### Two version axes, never one

The adapter reports the MCP protocol era and the heartbeat extension version
as **separate fields** of `EraReport`, at both ends of the session:

```json
{"mcp_protocol_era": "2025-06-18", "extension_version": "0.1", "heartbeat_supported": true}
```

A `2025-06-18` session with `heartbeat_supported: false` is a perfectly good
MCP session. It is not a degraded state and it is not an error.

## 2. Negotiation, and what happens when it fails

Two negotiations happen at `initialize`, and they fail differently on purpose.

**Protocol version — fatal.** Unsupported or ambiguous ends the session with a
JSON-RPC `INVALID_PARAMS` error and no negotiated value to echo. Both failures
are appended to the session's disagreement ledger and surfaced by
`LegacyServerSession.stats()`.

**Heartbeat capability — not fatal.** A peer that advertises no heartbeat
capability is an ordinary MCP peer. The session completes, ordinary methods
work unchanged, and the peer is reported heartbeat-unsupported. This is the
"unknown peers continue ordinary MCP behavior" acceptance criterion, and it is
the reason the two negotiations are separate functions rather than one.

Negotiation is **bilateral**. The extension is in effect only if the peer
offered it *and* this side can serve it; neither end acquires the extension by
reading the other's advertisement alone.

Ambiguity always fails closed. A peer advertising both the legacy
`experimental.presenceLease` identifier and the modern
`com.dougfirlabs/heartbeat` one is refused with `capability_ambiguous`: a
legacy connection may not silently claim current MCP semantics.

## 3. Defect closure matrix

Four defects from the immutable HB-00 register — held, with its
repair disposition, in the originating repository — are assigned to
this adapter. Each is inverted by positive **and** negative vectors in the
repaired corpus.

| id | defect at baseline | repair | vectors |
| --- | --- | --- | --- |
| D-02 | `initialize` advertised `resources.subscribe=true` with no handler | the advertisement is *derived* from the method registry (`capabilities.advertise`), so it cannot claim an unserved method; `agreement_violations` audits both directions | 2 positive, 4 negative |
| D-03 | `notifications/initialized` neither sent nor handled | the client sends it, the server handles it, and it is load-bearing: every ordinary request before it is refused with `session_not_initialized` | 1 positive, 3 negative |
| D-04 | server echoed any `protocolVersion`, recorded nothing | closed supported set; unsupported/ambiguous produce **no** negotiated value and fail closed; every disagreement is recorded in the ledger and reported by `stats()` | 2 positive, 5 negative |
| D-10 | `{uri, revision, digest}` sent under `notifications/resources/updated`, whose legacy params are `{uri}` only | the standard notification carries `{uri}` and nothing else; revision metadata moved to the namespaced `notifications/experimental/presenceLease/updated`; correctness moved off the hint entirely | 2 positive, 5 negative |

### D-10 deserves the long version

The archived defence compared a hint's digest against the refetched lease.
A conformant peer sends `{uri}`, so there was no digest, so the defence
evaluated to nothing — silently, with no violation code. Both ends of the lab
spoke the overloaded dialect, which is why 12/12 Docker scenarios passed.

The repair is not "put the digest back". It is:

1. **the hint stops carrying the guarantee.** A hint's only legal effect is to
   schedule a refetch. Correctness comes from `mcp_heartbeat.lineage.admit`
   over the *refetched* document, which a forged hint cannot influence;
2. **digest comparison survives as corroboration**, reported as
   `True` / `False` / `None`, and `None` is **counted**
   (`LegacyHeartbeatConsumer.uncorroborated_hints`) rather than ignored;
3. **metadata under the standard method name is dropped, not trusted**, and
   the drop is flagged as `overloaded_standard_method`. A conformant peer's
   `{uri}` and the lab's overloaded payload are indistinguishable in intent,
   so the adapter declines to guess.

A `{uri}`-only hint therefore still converges. That is the acceptance test.

## 4. Identity binding

The legacy contract has no per-message signer. The strongest authenticated
context it exposes is the **session** principal established at connection
time, and that is what the adapter binds to — named explicitly in the
evidence rather than implied:

```json
{"identity_binding": "bound", "principal": "spiffe://acme/ns/prod/sa/worker",
 "context_source": "session_token", "participant_id": "acme/worker-1"}
```

* `bound` — a principal was determined and the injected mapping permits it to
  publish that participant.
* `unbound` — a principal was determined and the mapping does **not** permit
  it. Fails closed: the fetch is refused and the previously held lease is
  kept, however well-formed the new document was.
* `unverified` — no principal could be determined, or no binder was injected.
  The claim is unchecked, not disproved. Never a promotion.

Two boundaries hold here:

* **identity is a separate facet, not a verification rung** (operator decision
  D-N2). `LegacyFetchOutcome` reports `admission` and `identity` in separate
  fields; a document can be a valid next revision *and* refused because the
  publisher was not permitted to publish it. Publisher identity is never
  folded into content verification.
* **the principal → permitted-participant mapping is injected** by a
  presence service or another deployment owner (D-N3). `permitted` is a required
  keyword argument with no default. This package owns no policy table, and a
  test asserts it.

## 5. Known limitations

1. **No transport.** The adapter speaks method names and dictionaries. Framing,
   HTTP, SSE, authentication, and retry belong to the deployment.
2. **Session-scoped identity only.** Binding is no stronger than the session's
   authentication. Behind a gateway that terminates authentication and
   forwards no per-participant assertion, the honest answer is `unverified`,
   and that is what is returned. Per-message publisher proof is out of scope
   for the legacy era and is not planned for it.
3. **No modern primitives.** No `subscriptions/listen`, no `server/discover`,
   no per-request `_meta`, no `capabilities.extensions` identifier. Those are
   HB-03's, and the era boundary is enforced at runtime by
   `assert_legacy_era`.
4. **D-10 metadata requires the extension notification.** A peer that has not
   negotiated the extension never receives revision metadata and always
   refetches. This costs a round trip and is the correct trade.
5. **The archived lab is not this adapter.** `integration/mcp_presence_lab/`
   still contains the defective baseline. It is preserved deliberately (it is
   the HB-00 exhibit) and retargeting it is HB-05's call, not this PRD's.
6. **`2025-03-26` is negotiated but not separately conformance-tested.** It is
   accepted because clients ask for it; the corpus exercises `2025-06-18`
   semantics.

## 6. Corpus policy: two corpora, never merged

| corpus | location | mutability |
| --- | --- | --- |
| historical | held in the originating repository; not published | **immutable.** Records the baseline as it was. |
| repaired | `tests/legacy/corpus/legacy-repaired-1.json` | versioned. New expectations bump the corpus version; they never overwrite the historical ones. |

`test_defect_closure.py` pins the source of each HB-02-owned historical
reproducer by digest, per function rather than per file, so a later PRD
inverting *its own* defect in the same module does not trip the guard while
any edit to D-02/D-03/D-04's reproducers does.

**Gap recorded, not worked around:** the HB-00 register states every defect has
a reproducer, but **D-10 has none** — it is described only in prose. There is
therefore no historical behaviour to invert for D-10, only the repaired
corpus's vectors. A test asserts the absence, so adding one later forces a
cross-check.

## 7. Deprecation policy

* **Supported while it is useful, and no longer.** Legacy support exists for
  the existing lab and for older clients. It is not a commitment to indefinite
  maintenance, and this document is not a public promise — nothing here has
  been published.
* **Non-normative.** New implementations target the current adapter. Legacy
  mechanics must not be cited as the reference behaviour of the heartbeat
  extension.
* **Frozen surface.** The adapter's public surface is frozen at the corpus
  version above. Behaviour changes require a new corpus version and a defect
  or decision id justifying them.
* **Sunset requires a decision, not a release.** Withdrawing a protocol
  revision is an operator decision recorded in the epic, not a side effect of
  a refactor. When one is withdrawn, `SUPPORTED_PROTOCOL_VERSIONS` loses the
  entry and negotiation for it fails closed with `unsupported_version` — the
  same path as any unknown revision, so no client sees a new failure mode.
* **Unsafe quirks are not preserved.** HB-00 classified the four defects above
  as defects, not as behaviour to maintain. Bug-for-bug compatibility with the
  archived lab is explicitly not offered.

## 8. Known-client interoperability transcript

A complete exchange between `LegacyClientSession` and `LegacyServerSession`,
captured from the adapter (participant `acme/worker-1`, epoch `e1`, `FakeClock`
at `2026-01-01T00:00:00Z`):

```
C->S initialize
    {"capabilities": {"experimental": {"presenceLease": {"extension_version": "0.1"}}},
     "clientInfo": {"name": "mcp-lab-client", "version": "0.1.0"},
     "protocolVersion": "2025-06-18"}

S->C initialize result
    {"capabilities": {"experimental": {"presenceLease": {"extension_version": "0.1"}},
                      "resources": {"listChanged": false, "subscribe": true}},
     "protocolVersion": "2025-06-18",
     "serverInfo": {"name": "mcp-lab-server", "version": "0.1.0"}}

C->S notifications/initialized
    {}

--   era report
    {"extension_version": "0.1", "heartbeat_supported": true,
     "mcp_protocol_era": "2025-06-18"}

S->C notifications/resources/updated
    {"uri": "presence://acme/worker-1"}

S->C notifications/experimental/presenceLease/updated
    {"digest": "sha256:81472d529ccbd90a933d3ecbdee9d3efb39d56c3f809adf11195fdc6344bbdeb",
     "revision": "e1:0", "uri": "presence://acme/worker-1"}

C->S resources/read
    {"uri": "presence://acme/worker-1"}

--   fetch outcome
    {"accepted": true, "reason": null, "hint_corroborated": true,
     "identity_binding": "bound", "principal": "spiffe://acme/ns/prod/sa/worker",
     "context_source": "session_token", "participant_id": "acme/worker-1",
     "mcp_protocol_era": "2025-06-18", "extension_version": "0.1",
     "heartbeat_supported": true}
```

Note the two updated-notifications: the standard one carries `{uri}` and
nothing else, and the extension one — which only a peer that negotiated the
extension receives — carries the revision metadata. That separation is D-10's
repair in one frame.

## 9. Running the corpora

```sh
# repaired corpus (this adapter)
python -m pytest -q tests/legacy

# The historical corpus is the archived baseline from the originating
# repository. It is not published with this package, and the closure
# tests that compare against it skip when it is absent.

# the whole package, including the portable core
python -m pytest -q tests
```
