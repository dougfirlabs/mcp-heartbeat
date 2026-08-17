# Governance

`mcp-heartbeat` is published by **Doug Fir Labs, Inc.** under the
[MIT License](LICENSE). This document says who decides what, and what a
release has to clear before it goes out.

## Roles

| Role | Who | May |
| --- | --- | --- |
| Contributor | anyone | open issues and pull requests |
| Maintainer | [`MAINTAINERS`](MAINTAINERS) | triage, review, merge |
| Release authority | [`MAINTAINERS`](MAINTAINERS) | tag, publish, change the licence, accept a recorded budget overrun |

The split matters in one specific way: **merging is not releasing.** A
maintainer can merge a change; nothing about that merge tags a version,
uploads a distribution, or pushes an image. Those are separate,
deliberate acts by the release authority, and no CI workflow in this
repository performs them.

## Decisions

Ordinary changes are decided by maintainer review on the pull request.

Three kinds of change are reserved to the release authority, because each
one weakens a property the project sells:

1. **Growing the portable core past its recorded ceiling.** The ceiling
   and the budget live in [`docs/loc-budget.md`](docs/loc-budget.md), and
   an overrun is accepted by a signed-off record in
   `docs/core-size-signoff.json`. That record is keyed to the exact
   measurement it accepts and to the exact budget it was measured
   against, so it lapses the moment either number changes. Accepting an
   overrun is not raising the budget, and the conformance case re-opens
   on the next line.
2. **Adding a runtime dependency to the core, or widening what the wheel
   ships.** Both are checked against the built artifact by
   `tools/verify_wheel.py`, not against intent.
3. **Anything that changes the wire contract** — a new field, a changed
   admission rule, a changed error code. Extensions exist so that most
   proposals do not need this.

## What a release must clear

A release candidate is not a release until, at minimum:

- the package suite passes from a clean checkout;
- the isolated official-SDK lane passes (`tools/verify_sdk.sh`);
- `tools/verify_wheel.py` passes against a freshly built wheel and sdist;
- the conformance matrices report no FAIL and no unaccepted HOLD.

Publication itself — tagging, uploading a distribution, pushing an image
— is a manual act by the release authority. There is no workflow in
`.github/workflows/` that publishes anything, and adding one is a
release-authority decision, not a maintenance detail.

## Status of the extension identifier

The experimental extension identifier is `com.dougfirlabs/heartbeat`.

It is **experimental and vendor-namespaced on purpose**. It is not
registered with, endorsed by, or submitted to any standards body, and
`extension_version` versions *this contract* independently of any MCP
protocol revision. If the identifier or the scheme is ever standardised,
that will be a deliberate, announced change with a migration path — not a
silent rename.

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
Enforcement is a maintainer responsibility; see that document for the
reporting channel and the escalation ladder.

## Provenance

How this repository came to exist, and why its history starts where it
does, is in [`PROVENANCE.md`](PROVENANCE.md).
