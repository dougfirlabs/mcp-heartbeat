# Security policy

## What this project is, in security terms

`mcp-heartbeat` publishes and admits **liveness leases**. A heartbeat
asserts one thing: this participant was alive and publishing at
`issued_at`, and claims the window through `expires_at`. It does not
assert readiness, health, authorization, correctness, or task success.

**Freshness is not permission.** Any deployment that derives authority
from a fresh heartbeat has built an authorization bypass out of a
liveness signal, and no version of this library will stop it. The
adapters therefore treat an absent principal as `unverified` — never as
a promotion — and they hold no policy table and no default allow rule.
The principal → permitted-participant mapping is injected by the
deployment owner, on purpose: it is the deployment's decision, and this
package refuses to make it by default.

## Threat model

In scope:

- forged, replayed, reordered, or stale heartbeats admitted as fresh;
- a boot-id or sequence rule that lets a restarted participant resume a
  lineage it should not;
- an extension or unknown member that changes a verdict it should be
  safely ignorable from;
- a parsing or validation defect reachable from a hostile document;
- an identity-binding defect in either era adapter that turns
  `unverified` into `verified`.

Out of scope:

- transport security. This contract is transport-neutral; confidentiality
  and peer authentication belong to the transport you carry it over.
- authorization. See above — that is a deliberate non-goal, not a gap.
- denial of service against a participant that chooses to accept
  unbounded input before validating it.

## Reporting a vulnerability

**Do not open a public issue for a security report.**

Use GitHub's private vulnerability reporting on this repository:
**Security → Report a vulnerability**. That opens a private advisory
visible only to you and the maintainers listed in [`MAINTAINERS`](MAINTAINERS).

Please include:

- the version or commit you tested;
- a minimal reproducer — a heartbeat document and the sequence of
  `admit` calls is usually enough;
- what verdict you got and what verdict you expected;
- your assessment of impact.

## What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement | 3 working days |
| Initial assessment | 10 working days |
| Fix or documented mitigation | 90 days from acknowledgement |

This is a small project maintained on a best-effort basis and these are
targets, not contractual commitments. We will tell you if a date is going
to slip rather than let it pass quietly.

We will credit you in the advisory and the release notes unless you ask us
not to. We do not operate a bug-bounty programme.

## Supported versions

`mcp-heartbeat` is at **0.1**, an experimental extension. Only the latest
released version is supported; there is no backport branch. The extension
identifier `com.dougfirlabs/heartbeat` is experimental and not a
registered or standardised identifier — see
[`GOVERNANCE.md`](GOVERNANCE.md).
