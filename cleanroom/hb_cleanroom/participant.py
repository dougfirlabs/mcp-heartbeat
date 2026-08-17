"""An independent MCP Heartbeat 0.1 participant, written from the contract.

This module is a **clean-room** implementation. It was authored from the
normative artifacts only:

* ``schema/mcp-heartbeat-0.1.schema.json`` — field names, types, patterns,
  and the ``additionalProperties``/``patternProperties`` rules;
* ``docs/heartbeat-0.1.md`` — the prose contract;
* ``tests/fixtures/positive/`` and ``tests/fixtures/negative/`` — the
  conformance corpus, used as acceptance criteria.

It deliberately imports **nothing** from ``mcp_heartbeat``,
``mcp_heartbeat_legacy``, or ``mcp_heartbeat_current``, shares no source
with them, and lives outside ``src/`` so it is not even on the same
import path by accident. ``mcp_heartbeat_conformance.cleanroom`` asserts
all of that mechanically — the claim of independence is checked, not
asserted in a comment.

Where the reference implementation and this one disagree, that is a
finding about the *contract*: a specification that two independent
implementations read differently is under-specified, which is exactly
what an interoperability check is supposed to surface.

Nothing here is optimised. It is written to be obviously a direct
transcription of the schema, because its value is entirely in being an
independent reading of the spec rather than in being good code.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

#: The one contract version this participant speaks (schema: ``const``).
EXTENSION_VERSION = "0.1"

#: Required members, in the order the schema lists them.
REQUIRED_FIELDS: tuple[str, ...] = (
    "extension_version",
    "node_id",
    "boot_id",
    "sequence",
    "issued_at",
    "expires_at",
)

#: Members the schema names. Anything else must be namespaced.
KNOWN_FIELDS: frozenset[str] = frozenset(REQUIRED_FIELDS) | {"extensions"}

# Transcribed straight from the schema's `pattern` keywords.
NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,254}$")
BOOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: "no more than 3600s later", from the `expires_at` description.
MAX_LEASE_SECONDS = 3600.0


class CleanRoomViolation(ValueError):
    """A document that does not satisfy the contract, with every reason."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        super().__init__("; ".join(self.violations))


def parse_timestamp(raw: object) -> datetime | None:
    """Parse an RFC 3339 instant, or return ``None`` if it is not one.

    An offset is required: the schema says "RFC 3339 UTC instant", and a
    naive timestamp is ambiguous by exactly the amount that matters for
    an expiry check.
    """
    if not isinstance(raw, str):
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed


def validate(document: Mapping[str, Any]) -> list[str]:
    """Return every violation in ``document``; an empty list means valid.

    Complete violation sets rather than first-failure, because the
    conformance corpus asserts exact sets and a first-failure validator
    would make every negative fixture assert one arbitrary reason.
    """
    violations: list[str] = []
    if not isinstance(document, Mapping):
        return ["document must be a JSON object"]

    for name in REQUIRED_FIELDS:
        if name not in document:
            violations.append(f"missing required field: {name}")

    version = document.get("extension_version")
    if version is not None and version != EXTENSION_VERSION:
        violations.append(
            f"extension_version must be {EXTENSION_VERSION!r}, got {version!r}"
        )

    node_id = document.get("node_id")
    if node_id is not None and not (
        isinstance(node_id, str) and NODE_ID_PATTERN.match(node_id)
    ):
        violations.append("node_id does not match the contract pattern")

    boot_id = document.get("boot_id")
    if boot_id is not None and not (
        isinstance(boot_id, str) and BOOT_ID_PATTERN.match(boot_id)
    ):
        violations.append("boot_id does not match the contract pattern")

    sequence = document.get("sequence")
    if sequence is not None:
        # `isinstance(True, int)` is True in Python, and the schema says
        # integer, not boolean. Checked explicitly for that reason.
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            violations.append("sequence must be an integer")
        elif sequence < 0:
            violations.append("sequence must be >= 0")

    issued_at = parse_timestamp(document.get("issued_at"))
    expires_at = parse_timestamp(document.get("expires_at"))
    if "issued_at" in document and issued_at is None:
        violations.append("issued_at must be an RFC 3339 instant with an offset")
    if "expires_at" in document and expires_at is None:
        violations.append("expires_at must be an RFC 3339 instant with an offset")
    if issued_at is not None and expires_at is not None:
        window = (expires_at - issued_at).total_seconds()
        if window <= 0:
            violations.append("expires_at must be strictly after issued_at")
        elif window > MAX_LEASE_SECONDS:
            violations.append(
                f"the expiry window must be at most {MAX_LEASE_SECONDS:g}s, got {window:g}s"
            )

    extensions = document.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, Mapping):
            violations.append("extensions must be an object")
        else:
            for key in extensions:
                if "." not in str(key):
                    violations.append(f"extension key {key!r} is not namespaced")

    for key in document:
        if key not in KNOWN_FIELDS and "." not in str(key):
            violations.append(f"unknown member {key!r} is not namespaced")

    return violations


def canonical_json(document: Mapping[str, Any]) -> str:
    """A stable rendering, so two implementations digest identically."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def digest(document: Mapping[str, Any]) -> str:
    """``sha256:`` over the canonical rendering."""
    payload = canonical_json(document).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def is_fresh(document: Mapping[str, Any], now: datetime) -> bool:
    """Whether the lease still covers ``now``. Freshness is the whole claim."""
    expires_at = parse_timestamp(document.get("expires_at"))
    return expires_at is not None and now < expires_at


@dataclass
class CleanRoomIssuer:
    """Publishes one participant's heartbeat stream.

    The counter is strictly increasing and scoped to (participant,
    epoch), and it resets with the epoch — both taken from the
    ``sequence`` description in the schema.
    """

    node_id: str
    boot_id: str
    lease_seconds: float = 30.0
    sequence: int = -1

    def issue(self, now: datetime) -> dict[str, Any]:
        self.sequence += 1
        expires_at = now + timedelta(seconds=self.lease_seconds)
        return {
            "extension_version": EXTENSION_VERSION,
            "node_id": self.node_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "issued_at": _format(now),
            "expires_at": _format(expires_at),
        }

    def restart(self, boot_id: str) -> None:
        """Open a new epoch. The counter starts over, per the contract."""
        self.boot_id = boot_id
        self.sequence = -1


@dataclass
class CleanRoomConsumer:
    """Holds the newest admitted heartbeat for one participant.

    The lineage rules are read off the ``sequence`` and ``boot_id``
    descriptions: the counter is strictly increasing within an epoch,
    epochs are never reused, and a reset counter is legitimate only
    because the epoch changed.
    """

    node_id: str
    held: dict[str, Any] | None = None
    seen_epochs: set[str] = field(default_factory=set)

    def admit(self, document: Mapping[str, Any], now: datetime) -> str:
        """Admit or refuse one document; returns a reason code or ``ok``."""
        violations = validate(document)
        if violations:
            return "schema_invalid"
        if document["node_id"] != self.node_id:
            return "node_id_mismatch"
        if not is_fresh(document, now):
            return "expired_on_arrival"

        boot_id = document["boot_id"]
        held = self.held

        if held is None or boot_id != held["boot_id"]:
            if boot_id in self.seen_epochs:
                return "boot_id_reuse"
            self._hold(document)
            return "ok"

        if document["sequence"] < held["sequence"]:
            return "sequence_rollback"
        if document["sequence"] == held["sequence"]:
            if digest(document) == digest(held):
                return "duplicate"
            return "sequence_conflict"

        self._hold(document)
        return "ok"

    def _hold(self, document: Mapping[str, Any]) -> None:
        self.held = dict(document)
        self.seen_epochs.add(document["boot_id"])


def _format(moment: datetime) -> str:
    """RFC 3339 with an explicit ``Z``, millisecond precision."""
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


__all__ = [
    "BOOT_ID_PATTERN",
    "CleanRoomConsumer",
    "CleanRoomIssuer",
    "CleanRoomViolation",
    "EXTENSION_VERSION",
    "KNOWN_FIELDS",
    "MAX_LEASE_SECONDS",
    "NODE_ID_PATTERN",
    "REQUIRED_FIELDS",
    "canonical_json",
    "digest",
    "is_fresh",
    "parse_timestamp",
    "validate",
]
