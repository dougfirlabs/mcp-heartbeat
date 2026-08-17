# Provenance

## The history here is fresh, and that is deliberate

This repository's first commit is the first commit. There is no imported
history, no filtered branch, no subtree split, no graft.

`mcp-heartbeat` was written inside a private monorepo at Doug Fir Labs
before it was published. That monorepo's history carries build logs,
internal notes, unrelated proprietary work, and the ordinary exhaust of
private development. **No history-rewriting tool removes that** — `git
filter-branch`, `filter-repo` and `subtree split` re-package a history;
they do not audit it, and anything they miss ships with full provenance
metadata attached.

So the extraction copied **file content only**, and the public history
starts clean. What you lose is the commit-by-commit record of how the
package was written. What you gain is a repository where every byte was
looked at before it was published, which is the trade we would make
again.

## What was checked before publication

The staged tree was scanned, mechanically and as a **gate** rather than a
report, for:

- project codenames;
- import roots and module names belonging to the private stack;
- internal subsystem names;
- operator and account names, and non-role email addresses;
- private hostnames and private-range network addresses;
- absolute home-directory paths;
- build-system run identifiers and knowledge-base artefacts;
- credential-shaped material: tokens, assigned secrets, private-key
  blocks.

The scanner is not in this repository, and its absence is the point: a
scanner has to carry the vocabulary it searches for, so publishing it
would publish the list of names the scan exists to keep private. It runs
on the private side, over this tree, before this tree goes anywhere. The
categories above are the full list of what it checks; the counts are all
zero.

The same reasoning already governs
`src/mcp_heartbeat_conformance/isolation.py` in this repository: its
confidential-term scan takes the denied vocabulary as a *parameter* and
ships none of its own, and the leaks it checks unconditionally are
*shapes* — home paths, email addresses, private host identifiers,
credential-shaped assignments — which need no vocabulary at all.

## What is in this tree and what is in the distribution

They are not the same set, on purpose.

**Distributed** (one wheel, one sdist): `mcp_heartbeat` — the portable
core — plus the two era adapters `mcp_heartbeat_current` and
`mcp_heartbeat_legacy`. The core's declared runtime dependencies are
empty, and `import mcp_heartbeat` succeeds with nothing but the standard
library on the path.

**Repository-only**, present here but excluded from every artifact:

| Path | Why it does not ship |
| --- | --- |
| `src/mcp_heartbeat_conformance/` | the conformance matrices — a verifier shipped inside the artifact it verifies is checking itself |
| `cleanroom/` | an independent second implementation, written from the schema, prose and corpus alone; shipping it would make the "independent" check a dependency of the thing it checks |
| `tools/` | the distribution and SDK verification lanes |

That boundary is not a claim in this document. `tools/verify_wheel.py`
reads the built wheel's and sdist's file lists and asserts it, and
`tests/test_packaging.py` reports each of those checks as a test.

## Origin of the design

The contract, the corpus, and the conformance matrices were developed
together as a single body of work and are published together. The
clean-room implementation under `cleanroom/` carries its own provenance
statement in [`cleanroom/PROVENANCE.md`](cleanroom/PROVENANCE.md)
describing what its author was and was not permitted to read.

## Releases

How a release is approved, and what it must clear first, is in
[`GOVERNANCE.md`](GOVERNANCE.md).
