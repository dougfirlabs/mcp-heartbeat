# Two-participant demonstration

Unit tests prove the reducer is correct. They cannot prove the contract
survives a process boundary, a container boundary, a network hop, and a
participant that stops talking without saying so. That is what this is for.

Two containers on a private bridge network. One **publishes** a heartbeat
stream; the other **observes** it — holding lineage state and rendering a
verdict across a status transition, an expiry, and a recovery under a new
epoch.

> **Nothing here is published.** No image is pushed, no port leaves
> `127.0.0.1`, no external network is contacted, and the host is asserted back
> at its pre-run baseline when it finishes.

## Quick start

```bash
python run_demo.py                                   # run, verify, tear down
python run_demo.py --output ../release/demo-evidence.json
python run_demo.py --keep                            # leave it up for poking
```

Or by hand:

```bash
docker compose --profile observe build
docker compose up -d --wait hb-demo-publisher
docker compose --profile observe up hb-demo-observer
docker compose --profile observe down --volumes --remove-orphans
```

`--profile observe` is load-bearing on the way **down** as well as up: a
profile-gated service is invisible to a plain `docker compose down`, so
omitting it leaves the observer container and the evidence volume behind. The
baseline assertion in `run_demo.py` is what caught that, which is the argument
for having one.

## What the observer proves

| Scenario | Claim |
| --- | --- |
| `handshake` | A first heartbeat is admitted and opens an epoch at `sequence` 0 |
| `renewal` | Later heartbeats are admitted, the sequence rises strictly, the lease moves forward |
| `status-transition` | A status change is visible on a **refetched** document and does not affect the lease |
| `expiry` | A reachable but frozen participant's lease still lapses |
| `recovery` | A restart opens a new epoch, and the retired one can never come back |

The observer runs the **shipped** `mcp_heartbeat.admit()`. It implements no
admission logic of its own, because a demonstration whose oracle
re-implements the thing under test proves only that two copies of an idea
agree.

### The status transition is not a lease event

The publisher advertises `serving` / `draining` under the namespaced
`com.dougfirlabs/heartbeat` extension. The observer asserts that the flip is
visible on a refetched document **and** that the lease judgement is unchanged
by it: a draining participant is still fresh. Status is advisory metadata, not
an input to admission. *Freshness is not permission*, and neither is status.

### The expiry scenario is three claims, not one

This is the part that was wrong on the first live run, and the correction is
worth stating rather than hiding.

When the publisher is frozen it keeps answering and re-serves the document it
last minted — **byte for byte**. `admit()` tests duplication *before*
freshness, so the honest verdict for that redelivery is `duplicate`: neither a
transition nor an error, and carrying no reason code. Demanding
`expired_on_arrival` there would be asking for the wrong answer to the right
situation. So the scenario asserts all three separately:

1. the observer's **held** lease lapses with no notification arriving — what a
   consumer must do on its own, from its own state;
2. the redelivered document is recognised as an **idempotent duplicate**;
3. a **fresh** consumer, which has no duplicate to recognise, refuses those
   same bytes as `expired_on_arrival` — so a lapsed lease cannot be inherited.

`MAX_SKEW_SECONDS` is deliberately widened well past the lease. `check_freshness`
tests skew before expiry, so at the default bound the frozen document would be
refused as `clock_skew_exceeded` — a true statement about a different thing.
Narrowing it back would not make the demonstration stricter, only vaguer.

### `/healthz` is deliberately useless

The publisher exposes one, and it says so in its own response body. A green
`/healthz` from a frozen participant is the demonstration's point, not a bug
in it. The authoritative answer lives in the heartbeat document, and every
scenario asserts against a refetched document rather than against a signal.

## Security posture

Applied identically to both services through a YAML anchor, and — this is the
part that matters — **re-checked against the running containers with
`docker inspect`**, not read back off the Compose file. A Compose file records
an intention; whether the daemon applied it is a separate question, and only
the daemon is authoritative on it. Thirty-five checks across the two
containers land in the evidence record.

- non-root (`10001:10001`), read-only root filesystem, `tmpfs` for `/tmp`
- `cap_drop: [ALL]` with nothing added back, `no-new-privileges:true`, never privileged
- bounded CPU, memory and PIDs; explicit health check
- private bridge network, and attachment to *that network only*; no host networking
- host ports bound to `127.0.0.1` only
- no bind mounts at all: no Docker socket, no host home, no device maps
- one named volume `demo_evidence`, so `down --volumes` leaves no host state

Both images contain **no third-party packages** — `python:3.12-slim` pinned by
digest, plus source. There is no `pip install` step, so there is nothing to
pin, mirror, or trust beyond the base image digest. The core arrives through
the `hb_core` build context as a plain `mcp_heartbeat` package with no wheel
and no repository around it, which exercises the package's "standard library
only" claim across a real image boundary rather than asserting it.

## The host baseline

`run_demo.py` records container, volume and network **identifier sets** before
anything starts and compares them afterwards. Sets rather than counts: a run
that leaked one volume and removed another would balance out under counting
and be invisible.

**`docker compose down` never removes images**, by design — a re-run is fast
because they survive. The image delta is therefore *recorded* in the evidence
rather than asserted to zero. To return the host bit-for-bit to its pre-run
state:

```bash
docker compose --profile observe down --volumes --remove-orphans --rmi local
```

## Evidence

Each run writes a record conforming to
[`evidence.schema.json`](evidence.schema.json): the hardening posture read off
the live containers, per-scenario verdicts with every named check, the
transition and refusal ledgers, and the baseline before and after.

What it deliberately never contains: credentials, host paths, prompts, model
narrative, or unbounded logs. `tests/test_demo.py` asserts the schema and the
absence of forbidden keys against the committed record at
`../release/demo-evidence.json`.

Local runs write to `evidence/`, which is git-ignored. The committed record is
one reviewed run, regenerable with `--output`.

## Layout

```
docker-compose.yml       topology, hardening anchor, every parameter
publisher/               the publishing participant image + package
observer/                the observing participant image + package
run_demo.py              host-side driver: baseline, hardening, teardown
evidence.schema.json     the evidence contract
```

## Out of scope

External publication, registry push, image push, standards submission, and
production deployment are separate operator-gated decisions. The demonstration
proves the lease lifecycle across a container and network boundary; it is not
a performance benchmark and makes no throughput claim.
