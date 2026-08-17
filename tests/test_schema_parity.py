"""The JSON Schema and the Python validator must say the same thing.

The schema is what an external implementer reads; the validator is what this
package runs. They are two encodings of one contract, so they are pinned to
each other here — a field added to one and not the other is a test failure.
No JSON Schema library is used or required; this reads the document as data.
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp_heartbeat import EXTENSION_VERSION, REQUIRED_FIELDS
from mcp_heartbeat.model import _BOOT_ID_RE, _NODE_ID_RE

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schema" / "mcp-heartbeat-0.1.schema.json").read_text()
)


def test_the_schema_requires_exactly_the_validator_s_required_set() -> None:
    assert SCHEMA["required"] == list(REQUIRED_FIELDS)


def test_the_schema_pins_the_contract_version() -> None:
    assert SCHEMA["properties"]["extension_version"]["const"] == EXTENSION_VERSION


def test_the_identifier_patterns_match_the_validator_s() -> None:
    assert SCHEMA["properties"]["node_id"]["pattern"] == _NODE_ID_RE.pattern
    assert SCHEMA["properties"]["boot_id"]["pattern"] == _BOOT_ID_RE.pattern


def test_the_schema_declares_no_field_the_validator_does_not_know() -> None:
    assert set(SCHEMA["properties"]) == set(REQUIRED_FIELDS) | {"extensions"}


def test_the_schema_carries_no_health_readiness_or_work_field() -> None:
    forbidden = {"health", "accepting_work", "consistency", "operational_state",
                 "resource_pressure", "capabilities_digest", "tasks", "ready"}
    assert forbidden.isdisjoint(SCHEMA["properties"])


def test_the_schema_bounds_the_sequence_and_keeps_it_an_integer() -> None:
    sequence = SCHEMA["properties"]["sequence"]
    assert sequence["type"] == "integer" and sequence["minimum"] == 0


def test_the_schema_requires_extension_keys_to_be_namespaced() -> None:
    assert SCHEMA["properties"]["extensions"]["propertyNames"]["pattern"] == "\\."


def test_unknown_members_are_admitted_only_when_namespaced() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert "\\." in SCHEMA["patternProperties"]
