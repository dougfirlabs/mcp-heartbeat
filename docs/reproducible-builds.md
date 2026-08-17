# Reproducible builds

A checksum you cannot reproduce is a fingerprint of one machine, not a
property of the release. So the two release artifacts are built **twice, from
deliberately unalike inputs**, and the digests must agree. If they disagree
the release fails; the claim is not softened to "usually deterministic".

```sh
tools/build_release.py            # build twice, write release/
tools/build_release.py --check    # build twice, compare to committed, write nothing
sha256sum -c release/SHA256SUMS   # verify artifacts you already have
```

`--check` is the gate. It is what `tests/test_reproducibility.py` runs and
what CI runs, and it rebuilds from scratch rather than trusting `dist/`.

## What the two builds do differently

Building the same directory twice in a row agrees for reasons that say
nothing about a fresh clone on another host. The two passes therefore run
from **differently named directories**, so a build path embedded in an
artifact shows up as a mismatch rather than hiding.

The negative control matters as much as the gate. With the normalisation
below removed — divergent input mtimes, divergent file modes, no sdist
repack — *both* artifacts diverge. The gate can fail, which is why its
passing means something.

## The three sources of nondeterminism, and what fixes each

`SOURCE_DATE_EPOCH` is pinned to **1786838400** (`2026-08-16T00:00:00Z`) in
`tools/build_release.py`. It is a constant, not a value derived from the
clock or the git log, because anything derived is a different number on every
host. It moves when the release moves, by editing that line.

| Source | Reaches | Fix |
| --- | --- | --- |
| Source file mtimes | wheel **and** sdist | Inputs normalised to `SOURCE_DATE_EPOCH` before the backend runs |
| File permission bits | wheel (zip `external_attr`) | Inputs normalised to `0644` files / `0755` directories, so a contributor's umask cannot reach the artifact |
| Builder's username, generated-file mtimes, gzip header | sdist only | The sdist container is rewritten deterministically |

### The wheel needs no rewriting

setuptools' vendored `wheel` writer reads `SOURCE_DATE_EPOCH` when stamping
zip entries. Given normalised inputs the wheel comes out byte-identical
straight from the build backend, and `build_release.py` never touches it.
**The published wheel is the backend's own bytes.**

### The sdist container is rewritten, and proves it lost nothing

`distutils.archive_util.make_tarball` ignores `SOURCE_DATE_EPOCH` entirely.
It stamps build-time mtimes on the staging directory and the generated
`PKG-INFO`, copies the *builder's* username into every member's `uname` and
`gname` (via `tarfile.gettarinfo`), and lets `gzip` write a wall-clock
header. There is no backend option that fixes all three — `sdist --owner`
would cover only the second, and it is not reachable through
`python -m build`.

So the container is rewritten: members sorted by name, `uid`/`gid` set to `0`
with empty `uname`/`gname`, every mtime set to `SOURCE_DATE_EPOCH`, modes
normalised, and the gzip header written with `mtime=0` and no `FNAME` field.

This is metadata only, and the script does not ask to be trusted on that. It
reads the member payloads back out of the rewritten archive and compares them
byte-for-byte with what setuptools produced. A repack that dropped, added, or
altered a file raises there rather than shipping.

## What changes a digest

Legitimately: anything that lands in an artifact. That is wider than it looks
for the sdist, and one case surprises people —

**setuptools' default sdist file set includes top-level `tests/test*.py`.**
Adding or editing one of those changes the sdist digest even though nothing in
`src/` moved and the wheel is untouched. That is correct, not a leak; it just
means `release/SHA256SUMS` has to be regenerated after a test lands, and a
digest that moved "for no reason" usually has this reason.

The committed checksums cover the artifacts only. `release/` is excluded from
the build tree entirely, so `SHA256SUMS` can never end up inside the sdist it
describes.

## The caveat that is part of the claim

**Reproducible given the recorded toolchain.** A different Python or
setuptools may emit different bytes and still be correct — archive layout is
not a stability guarantee either project makes. `release/reproducibility.json`
records the exact versions used, and they are part of the claim rather than
incidental to it.

To reproduce the committed digests, match the `toolchain` block in that file.

## What is committed, and what is not

| Path | Committed | Why |
| --- | --- | --- |
| `release/SHA256SUMS` | yes | `sha256sum -c` compatible, with the recipe in its header |
| `release/sbom.json` | yes | CycloneDX 1.5, derived from the built wheel's `METADATA` |
| `release/reproducibility.json` | yes | The evidence record: both builds, both digests, the toolchain |
| `release/RELEASE-NOTES-0.1.0.md` | yes | **Drafted.** No tag, no release object, nothing published |
| `dist/*.whl`, `dist/*.tar.gz` | **no** | Build output, not source. Regenerate them; do not review a binary in a diff |

`release/` holds *statements about* the artifacts. `dist/` holds the
artifacts, and is git-ignored.

## Related

- `tools/verify_wheel.py` — what is *in* the artifact (shipped packages,
  withheld tooling, import purity against the installed wheel). A different
  question from this one, and both have to hold.
- `docs/hb05-release-gate.md` — the cross-era conformance verdict.
