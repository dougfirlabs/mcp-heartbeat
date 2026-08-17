# mcp-heartbeat 0.1.0 — release notes (**DRAFT**)

> **This is a draft, and nothing has been released.** No git tag exists, no
> GitHub release object exists, nothing has been uploaded to PyPI or any other
> registry, no container image has been pushed, and the repository's visibility
> has not been changed. Publishing is an operator decision that this document
> exists to inform, not to record. Everything below describes a **release
> candidate** built and verified locally.

## What this is

A small, transport-neutral **liveness-lease contract** for MCP participants,
plus adapters that bind it to two MCP protocol eras.

A heartbeat asserts exactly one thing:

> this participant was alive and publishing at `issued_at`, and claims the
> window through `expires_at`.

It does **not** assert readiness, health, authorization, correctness, or task
success. **Freshness is not permission.** There is deliberately no
`can_dispatch` API anywhere in the package, and there will not be one — that
absence is a design commitment, not an unfinished feature.

The whole wire object is six mandatory fields:

```json
{
  "extension_version": "0.1",
  "node_id": "svc/api-7",
  "boot_id": "3f2a91c0",
  "sequence": 12,
  "issued_at": "2026-01-01T00:00:00.000Z",
  "expires_at": "2026-01-01T00:00:30.000Z"
}
```

Optional data goes under `extensions` with namespaced keys and is safely
ignorable: discarding all of it reaches the same verdict.

## What ships

One wheel carrying three packages:

| Package | What | Needs |
| --- | --- | --- |
| `mcp_heartbeat` | The reference implementation of the contract | The standard library |
| `mcp_heartbeat_legacy` | The MCP `2025-06-18` adapter | The standard library |
| `mcp_heartbeat_current` | The MCP `2026-07-28` adapter | The standard library; the official SDK only at the `sdk` seam |

```sh
pip install mcp-heartbeat            # all three, no third-party dependency
pip install mcp-heartbeat[current]   # + the official SDK, for the sdk seam only
```

**The base install declares no runtime dependency at all**, and that is
enforced rather than intended: `tests/test_purity.py` asserts it at the AST
level and by importing in a subprocess with site-packages disabled, and
`tools/verify_wheel.py` asserts it against the *built* wheel, installed into a
throwaway virtualenv with `--no-index` under a meta-path guard that refuses
`mcp`, `mcp_types` and `pydantic`. Source purity and artifact purity are
different claims; only the second is what a user gets.

Without the `current` extra, `import mcp_heartbeat_current` still succeeds.
The seam raises `SdkUnavailable` naming the pins to install, rather than a
`ModuleNotFoundError` from three frames down.

What deliberately does **not** ship: `mcp_heartbeat_conformance` (the HB-05
matrices), `tools/` (the conformance tooling), and `cleanroom/hb_cleanroom/`
(an independent second implementation). A verifier shipped inside the artifact
it verifies is checking itself.

## Supported MCP eras

| Revision | Support | Adapter |
| --- | --- | --- |
| `2026-07-28` | yes | `mcp_heartbeat_current` — `server/discover`, per-request `_meta`, `subscriptions/listen` |
| `2025-06-18` | yes | `mcp_heartbeat_legacy` — the adapter's default |
| `2025-03-26` | negotiated | `mcp_heartbeat_legacy` — accepted when a client asks for it explicitly; **not separately conformance-tested** |

`extension_version` versions *this contract* and is a **separate axis** from
the MCP protocol revision. The same `0.1` heartbeat rides a `2025-06-18`
transport and a `2026-07-28` transport unchanged. Confusing the two axes is
the most likely way to misread this release.

Cross-era messages that would be plausible to *both* eras are refused by both,
and the classification happens **before** dispatch — so a cross-era refusal
cannot have renewed, republished, or resequenced a lease first.

## Provisional and experimental status — read this before depending on it

Three separate caveats, none of which is a formality:

1. **The extension identifier `com.dougfirlabs/heartbeat` is experimental and
   vendor-namespaced.** It is **not** registered with, endorsed by, submitted
   to, or under review by any standards body. It is a vendor namespace chosen
   so that it cannot collide with a future standardised name, precisely
   because no claim on such a name is being made.

2. **The wire naming is provisional.** `node_id` and `boot_id` keep their wire
   spelling at `0.1`, but *participant* and *epoch* are the normative terms and
   are what the API uses (`Heartbeat.participant_id`, `Heartbeat.epoch_id`). A
   wire rename is **deferred to `extension_version` 1.0** and should be
   expected there.

3. **Nothing here is standardised MCP.** The `0.1` in `extension_version` is
   meant literally: the contract may change, and a `1.0` is not promised on any
   schedule.

Identity is **claimed, never proven**: `IdentityClaim.authenticated` is
unconditionally `False`. Binding a claim to a real principal is a transport
adapter's job, reported as `bound` / `unbound` / `unverified` — never
collapsed to a boolean.

## Verification in this candidate

| Lane | What it proves |
| --- | --- |
| `python -m pytest` | The contract, the corpus, both adapters, source purity |
| `tools/verify_wheel.py` | What the built-and-installed artifact ships, withholds, and requires |
| `tools/build_release.py --check` | The wheel and sdist rebuild byte-for-byte identically |
| `tools/verify_sdk.sh` | The `2026-07-28` adapter against the pinned official SDK |
| `demo/run_demo.py` | Two containerised participants observing each other's leases |
| `tools/emit_hb05_evidence.py` | The six cross-era conformance matrices |

### Reproducible artifacts

Both artifacts are built twice from deliberately unalike inputs and the digests
must agree; `release/SHA256SUMS` and `release/reproducibility.json` record the
result and the toolchain the claim is scoped to. The wheel is the build
backend's own bytes. The sdist container is rewritten deterministically because
setuptools ignores `SOURCE_DATE_EPOCH` there, and the rewrite is proven
content-preserving rather than assumed to be. See
[`docs/reproducible-builds.md`](../docs/reproducible-builds.md).

### SBOM

`release/sbom.json` (CycloneDX 1.5) is derived from the built wheel's
`METADATA`, not from `pyproject.toml`. Every component is `scope: optional` and
names the extra that pulls it in, because **the base install pulls in nothing**.
Components carry a version only where the wheel pins one with `==`; a range is
recorded as a specifier rather than resolved to a guess.

### The demonstration

`demo/` runs two participants in separate containers over a private network,
where one observes the other's lease across a status transition, an expiry, and
a recovery under a new epoch. Hardening is asserted with `docker inspect`
against the **running** containers rather than read off the Compose file, and
the host is asserted back at its pre-run baseline after teardown. See
[`demo/README.md`](../demo/README.md).

## Known limitations

- `2025-03-26` is negotiated but not separately conformance-tested; the corpus
  exercises `2025-06-18`.
- The `current` adapter's SDK conformance lane needs an isolated environment
  (`tools/verify_sdk.sh`); installing the pinned SDK into a host application's
  shared virtualenv resolves `pydantic-core` off the version its `pydantic`
  pins.
- Reproducibility is scoped to the recorded toolchain. A different Python or
  setuptools may emit different bytes and still be correct.
- The demonstration proves the lease lifecycle across a container and network
  boundary. It is not a performance benchmark and makes no throughput claim.

## Not in this release, and gated elsewhere

External publication, PyPI upload, registry or image push, repository
visibility changes, standards submission, and production activation are each
separate operator-gated decisions. None of them has been taken, and none of
them is implied by this document.

## Licence and provenance

MIT — see [`LICENSE`](../LICENSE). Where this came from, and what was
deliberately left behind, is in [`PROVENANCE.md`](../PROVENANCE.md).
Security policy — including the fact that **freshness is not permission** — is
in [`SECURITY.md`](../SECURITY.md).
