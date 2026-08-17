# Contributing

Thank you for looking. This is a small, deliberately small project, and
the smallness is the feature — so the most useful thing to know before
you start is what will and will not be accepted.

## The one rule that decides most pull requests

**The portable core is a budget, not a canvas.** `src/mcp_heartbeat/` has
a recorded logical-LOC ceiling, enforced by
`mcp_heartbeat_conformance.isolation` and documented in
[`docs/loc-budget.md`](docs/loc-budget.md). A change that grows it needs
to say what it buys, and "it would be convenient" does not clear the bar.
The budget exists because the contract's value is that an implementer can
read all of it.

Corollaries, all of them tested rather than asked for politely:

- **the core imports only the standard library.** Not "only small
  dependencies" — only the standard library. `tests/test_purity.py`
  checks the source, and `tools/verify_wheel.py` checks the built wheel
  from inside a throwaway virtualenv, because those are different claims.
- **adapters may import the core; the core may not import an adapter.**
  Enforced at the AST level, so a forbidden import fails even on a code
  path no test exercises.
- **only `mcp_heartbeat_current/sdk.py` may touch the official MCP SDK.**
  Everything else reaches the SDK through that one module, so an absent
  SDK degrades one import rather than breaking the package.
- **the conformance package, `tools/` and `cleanroom/` do not ship.** A
  verifier inside the artifact it verifies is checking itself.

## What is welcome

- defect reports with a reproducing heartbeat document;
- test vectors — especially negative ones — for
  `tests/fixtures/`;
- independent implementations, and disagreements between yours and this
  one. A disagreement is a bug in the specification until proven
  otherwise;
- documentation that makes the contract easier to implement from scratch;
- portability fixes for supported Python versions.

## What is unlikely to be accepted

- new wire fields. Six fields is the design. Optional data goes under
  `extensions` with a namespaced key and must be safely ignorable —
  discarding all of it must reach the same verdict;
- readiness, health, or authorization semantics. See
  [`SECURITY.md`](SECURITY.md);
- a runtime dependency in the core, for any reason;
- transports, servers, schedulers, or background threads. The core reads
  time only through an injected clock, starts no thread, opens no socket,
  and persists nothing.

## Working on it

```sh
python -m pytest -q tests      # the package suite
tools/verify_wheel.py          # the distribution lane: builds and probes the wheel
tools/verify_sdk.sh            # the SDK conformance lane, in its own virtualenv
```

The tests put `src` on the path themselves, so no install step is needed.
The SDK lane builds a dedicated virtualenv because the pinned SDK's
`pydantic-core` resolution conflicts with what many host applications
already have installed — that is why it is a separate lane rather than an
extra dependency.

Style follows what is already there: prose docstrings that say *why*,
tests named as sentences, and comments that explain a decision rather
than restate the code.

## Provenance and licensing of contributions

By opening a pull request you agree that your contribution is licensed
under the [MIT License](LICENSE), and that you have the right to license
it. We do not require a CLA.

Please do not paste code from another project unless its licence permits
it and you say so in the pull request. This repository's history is
audited — see [`PROVENANCE.md`](PROVENANCE.md) — and keeping it that way
is easier than fixing it later.

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
