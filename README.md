# mcp-heartbeat

A small, transport-neutral liveness-lease contract for MCP participants.

A heartbeat asserts exactly one thing:

> this participant was alive and publishing at `issued_at`, and claims the
> window through `expires_at`.

It does not assert readiness, health, authorization, correctness, or task
success. **Freshness is not permission.**

## The whole wire object

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

Six fields. Optional data goes under `extensions` with namespaced keys and is
safely ignorable — discarding all of it reaches the same verdict.

## Usage

```python
from mcp_heartbeat import HeartbeatIssuer, LineageState, SystemClock, admit

# Producer: one issuer per (participant, epoch). A new process = a new epoch.
issuer = HeartbeatIssuer(participant_id="svc/api-7", clock=SystemClock())
document = issuer.issue().to_dict()

# Consumer: hold state per participant and admit candidates against it.
state = LineageState(participant_id="svc/api-7")
outcome = admit(state, document, SystemClock().now())
if outcome.accepted:
    state = outcome.state
else:
    print("refused:", outcome.reason)   # e.g. sequence_rollback, boot_id_reuse
```

The core reads time only through an injected clock, starts no thread, opens no
socket, and persists nothing. `FakeClock` makes every case deterministic:

```python
from mcp_heartbeat import FakeClock
clock = FakeClock()
clock.advance(30)     # both clocks move
clock.skew_wall(10)   # only the wall clock moves — an NTP step
```

## Design notes

- `extension_version` versions *this contract*, independent of the MCP
  protocol revision. The same 0.1 heartbeat rides a 2025-06-18 transport and a
  2026-07-28 transport unchanged.
- `node_id` and `boot_id` keep their wire spelling at 0.1. *Participant* and
  *epoch* are the normative terms and the names the API uses
  (`Heartbeat.participant_id`, `Heartbeat.epoch_id`); a wire rename is
  deferred to `extension_version` 1.0.
- **Identity is claimed, never proven.** `IdentityClaim.authenticated` is
  unconditionally `False`. Binding a claim to a real principal is a transport
  adapter's job, reported as `bound` / `unbound` / `unverified` — never
  collapsed to a boolean.
- Adapters plug in through four `typing.Protocol` ports
  (`HeartbeatPublisher`, `HeartbeatSource`, `HintReceiver`, `IdentityBinder`).
  The core implements none of them and imports no transport.

## The legacy MCP adapter

`src/mcp_heartbeat_legacy/` binds the core to MCP `2025-06-18` lifecycle,
capability, resource, and notification mechanics. It is a **sibling** of the
core, not part of it: the core stays era-free, and the adapter is the only
place a legacy method name appears. It ships in the same wheel, and needs
nothing the core does not — no extra, no optional dependency.

```python
from mcp_heartbeat_legacy import LegacyClientSession, LegacyServerSession

server = LegacyServerSession(server_name="lab", implemented={"resources/read"})
client = LegacyClientSession(client_name="probe")

result = server.handle(*client.initialize_request())
report = client.consume_initialize_result(result)
server.handle(*client.initialized_notification())

report.mcp_protocol_era   # '2025-06-18'   — the MCP era
report.extension_version  # '0.1'          — the heartbeat contract. Separate axes.
```

It repairs four HB-00 defects (D-02, D-03, D-04, D-10) against a separately
versioned corpus, without touching the archived baseline. Supported revisions,
identity binding, known limitations, and the deprecation policy are in
[`docs/legacy-compatibility.md`](docs/legacy-compatibility.md).

## Dependencies

The Python standard library, and nothing else — no host application, web
framework, MCP SDK, or JSON Schema validator. Declared
`dependencies` are empty and stay empty; the current adapter's SDK is an
optional extra.

Proven three ways, deliberately independent. `tests/test_purity.py` asserts
it at the AST level and by importing the source in a subprocess with
site-packages disabled. `tools/verify_wheel.py` asserts it against the
*built* artifact: it builds the wheel, installs it into a throwaway
virtualenv with `--no-index`, and imports the package there under a guard
that refuses `mcp`, `mcp_types` and `pydantic` at the meta-path. Source purity and artifact purity are
different claims, and only the second one is what a user gets.

## Layout

| Path | What |
| --- | --- |
| `docs/heartbeat-0.1.md` | the normative contract |
| `docs/current-adapter-2026-07-28.md` | the MCP 2026-07-28 adapter |
| `docs/loc-budget.md` | core size measurement and its justification |
| `docs/hb05-release-gate.md` | the cross-era conformance verdict and residual risks |
| `docs/legacy-compatibility.md` | legacy support statement and deprecation policy |
| `docs/reproducible-builds.md` | how the artifacts are built twice, and why that is a gate |
| `schema/mcp-heartbeat-0.1.schema.json` | the machine-readable contract |
| `src/mcp_heartbeat/` | the reference implementation (**shipped**) |
| `src/mcp_heartbeat_legacy/` | the MCP `2025-06-18` adapter (**shipped**) |
| `src/mcp_heartbeat_current/` | the MCP `2026-07-28` adapter (**shipped**) |
| `src/mcp_heartbeat_conformance/` | the HB-05 conformance matrices (not distributed) |
| `cleanroom/hb_cleanroom/` | an independent participant, built from the contract alone (not distributed) |
| `tests/fixtures/` | the positive/negative conformance corpus |
| `tests/legacy/corpus/` | the repaired legacy corpus, separately versioned |
| `tools/verify_wheel.py` | builds, installs and probes the distribution (not distributed) |
| `tools/verify_sdk.sh` | runs the adapter's SDK conformance lane |
| `tools/emit_hb05_evidence.py` | regenerates the cross-era evidence pack |
| `tools/build_release.py` | builds both artifacts twice and proves the digests agree (not distributed) |
| `demo/` | the two-participant Docker demonstration (not distributed) |
| `release/` | checksums, SBOM, drafted notes, demonstration evidence — **candidate, nothing published** |

## Adapters

The core speaks no transport. Adapters bind it to a specific MCP revision
and live *beside* the core rather than inside it — separate packages,
importing inwards only, so the core stays era-free. Both ship in the same
wheel: `pip install mcp-heartbeat` gives you all three.

That costs nothing, which is the point. The adapters are standard library
plus the core too, so shipping them widens the *distribution* without
widening the *dependency* — `import mcp_heartbeat` still works in an
interpreter with no third-party distribution installed at all.

`mcp_heartbeat_current` binds MCP revision `2026-07-28` —
`server/discover`, per-request `_meta`, `subscriptions/listen`, and the
`com.dougfirlabs/heartbeat` extension identifier. Only its `sdk.py`
imports the official SDK, and that SDK is an optional extra
(`pip install mcp-heartbeat[current]`). Without the extra,
`import mcp_heartbeat_current` still succeeds; the seam raises
`SdkUnavailable` naming the pins to install, rather than a
`ModuleNotFoundError` from three frames down.

`mcp_heartbeat_legacy` covers MCP `2025-06-18` for compatibility and
needs no extra at all.

See [`docs/current-adapter-2026-07-28.md`](docs/current-adapter-2026-07-28.md)
and [`docs/legacy-compatibility.md`](docs/legacy-compatibility.md).

## Conformance

`mcp_heartbeat_conformance` runs six matrices across both adapters —
cross-era pairs, distributed-runtime shapes, identity binding, clean-room
provenance, measured overhead, and package isolation — then adjudicates
release readiness at three independent levels.

```sh
python tools/emit_hb05_evidence.py --output docs/evidence/mcp-heartbeat-hb05
```

Exit `0` when every matrix clears; `1` when something is on HOLD or FAILed
and an operator has to look. Verdicts are four-valued: `PASS`, `FAIL`,
`UNSUPPORTED` (this topology could not express the case) and `HOLD`
(measured, outside threshold, and therefore someone's decision). The extra
two exist so the pack can distinguish "we proved it works" from "we could
not run it" from "it works but a decision is owed".

The package depends on the core and both adapters and nothing depends on
it, so it can be deleted without touching what it verifies. Current
verdict and open items:
[`docs/hb05-release-gate.md`](docs/hb05-release-gate.md).

`cleanroom/hb_cleanroom/` is a second implementation written from the
schema, prose, and corpus alone. It imports nothing from the reference,
lives outside `src/`, and ships in no wheel; its independence is checked
mechanically rather than asserted —
[`cleanroom/PROVENANCE.md`](cleanroom/PROVENANCE.md).

## Tests

```sh
python -m pytest -q tests                          # from the repository root
python -m pytest -q                                # from this directory
tools/verify_wheel.py                              # + the distribution lane
tools/verify_sdk.sh                                # + the SDK conformance lane
```

The tests add `src` to the path themselves, so no install step is needed and
no host application is imported. The adapter's SDK conformance tests skip unless
the pinned official SDK is installed; `verify_sdk.sh` builds an isolated
environment for them, because installing that SDK into a host
application's shared virtualenv would move `pydantic-core` off the version
its `pydantic` pins.

`verify_wheel.py` is the distribution lane and runs standalone or through
`tests/test_packaging.py`, which reports each of its checks as a test. It
builds into a temporary directory and never touches the calling
environment, for the same reason: the venv it installs into is thrown
away with it.

## The demonstration

Unit tests prove the reducer is correct. They cannot prove the contract
survives a process boundary, a container boundary, a network hop, and a
participant that stops talking without saying so.

```sh
python demo/run_demo.py
```

Two containers on a private bridge network — one publishing a heartbeat
stream, one holding lineage state and watching it across a status
transition, an expiry, and a recovery under a new epoch. The observer runs
the **shipped** `admit()` rather than its own copy of the rules.

Container hardening is re-read off the **running** containers with
`docker inspect` rather than trusted from the Compose file, and the host is
asserted back at its pre-run baseline afterwards. Loopback only; no image is
pushed anywhere. See [`demo/README.md`](demo/README.md).

## Release artifacts

```sh
tools/build_release.py --check       # rebuild both artifacts and compare
sha256sum -c release/SHA256SUMS      # verify artifacts you already have
```

The wheel and the sdist are built **twice**, from deliberately unalike
inputs, and the digests have to agree — with each other and with the
committed `release/SHA256SUMS`. The wheel is the build backend's own bytes;
the sdist container is rewritten deterministically because setuptools ignores
`SOURCE_DATE_EPOCH` there, and that rewrite proves it lost nothing rather
than asking to be trusted. [`docs/reproducible-builds.md`](docs/reproducible-builds.md)
has the three sources of nondeterminism and what fixes each.

`release/sbom.json` (CycloneDX 1.5) is derived from the built wheel's
`METADATA`, not from `pyproject.toml`. Every component is `scope: optional`
and names the extra that pulls it in, because the base install pulls in
nothing.

> `release/` holds a **candidate**. `release/RELEASE-NOTES-0.1.0.md` is a
> draft: there is no tag, no release object, and nothing has been uploaded to
> any registry. Publication is a separate operator-gated decision — see
> [`GOVERNANCE.md`](GOVERNANCE.md).

## Project

| | |
| --- | --- |
| Licence | [MIT](LICENSE) |
| Security policy | [`SECURITY.md`](SECURITY.md) — and note that **freshness is not permission** |
| Contributing | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Code of conduct | [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) |
| Who decides what | [`GOVERNANCE.md`](GOVERNANCE.md), [`MAINTAINERS`](MAINTAINERS) |
| Where this came from | [`PROVENANCE.md`](PROVENANCE.md) |

The extension identifier `com.dougfirlabs/heartbeat` is **experimental**
and vendor-namespaced: it is not registered with, endorsed by, or
submitted to any standards body.
