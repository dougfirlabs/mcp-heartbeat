"""Generate the HB-03 evidence pack.

The PRD requires four artifacts: official SDK version/provenance, a current
protocol transcript, a forbidden-primitive lint, and a subscription fault
matrix. Each is produced here from the *running* adapter rather than
transcribed by hand, so an artifact cannot describe a version of the code
that no longer exists.

Run through ``tools/verify_sdk.sh --evidence`` so the SDK-derived artifacts
come from the pinned SDK in its isolated environment. Without the SDK the
transcript and provenance are emitted with ``"available": false`` rather
than silently omitted — a missing artifact and an unavailable one are very
different claims.

    python tools/emit_evidence.py --output docs/evidence/mcp-heartbeat-hb03
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from mcp_heartbeat.clock import FakeClock  # noqa: E402
from mcp_heartbeat.issuer import HeartbeatIssuer  # noqa: E402
from mcp_heartbeat.ports import ChangeHint  # noqa: E402

from mcp_heartbeat_current import contract, sdk  # noqa: E402
from mcp_heartbeat_current.convergence import (  # noqa: E402
    TOPOLOGY_RULES,
    Convergence,
    FetchResult,
    HeartbeatConsumer,
)
from mcp_heartbeat_current.discovery import (  # noqa: E402
    HeartbeatCapability,
    build_discover_request,
    build_discover_result,
    build_read_request,
    negotiate,
)
from mcp_heartbeat_current.lint import lint_package  # noqa: E402
from mcp_heartbeat_current.metadata import classify_request  # noqa: E402
from mcp_heartbeat_current.subscriptions import (  # noqa: E402
    SubscriptionFilter,
    SubscriptionStream,
    build_acknowledgement,
    build_resource_updated,
)

PARTICIPANT = "svc/api-7"


def _redact(headers: dict[str, str]) -> dict[str, str]:
    """Headers are protocol metadata here, but never assume it."""
    banned = ("authorization", "cookie", "token", "secret")
    return {k: v for k, v in headers.items() if not any(b in k.lower() for b in banned)}


def protocol_transcript() -> dict:
    """A full modern exchange, captured message by message.

    Discovery, the authoritative read, and a subscription — with the header
    set beside each body, because the ``_meta``/header mirroring is half of
    what makes the exchange conformant and is invisible in a body-only log.
    """
    clock = FakeClock()
    issuer = HeartbeatIssuer(
        participant_id=PARTICIPANT, epoch_id="epoch-a", clock=clock, lease_seconds=30.0
    )
    heartbeat = issuer.issue()

    discover_body, discover_headers = build_discover_request(request_id="req-1")
    discover_result = build_discover_result(
        server_info={"name": "mcp-heartbeat-current", "version": "0.1.0"},
        capability=HeartbeatCapability(),
    )
    negotiated = negotiate(discover_result)
    read_body, read_headers = build_read_request(PARTICIPANT, negotiated, request_id="req-2")

    uri = negotiated.capability.resource_uri(PARTICIPANT)
    listen_filter = SubscriptionFilter.for_participants([uri])
    ack = build_acknowledgement("listen-1", listen_filter)
    hint = ChangeHint.for_heartbeat(heartbeat, address=uri)
    updated = build_resource_updated(uri, hint, subscription_id="listen-1")

    stream = SubscriptionStream(subscription_filter=listen_filter)
    stream.accept(ack)
    stream.accept(updated)

    return {
        "artifact": "current-protocol-transcript",
        "protocol_revision": contract.PROTOCOL_REVISION,
        "extension_id": contract.HEARTBEAT_EXTENSION_ID,
        "extension_version": contract.HEARTBEAT_EXTENSION_VERSION,
        "note": (
            "No initialize, no notifications/initialized, no Mcp-Session-Id, no "
            "GET stream, no resources/subscribe. Discovery is one request; change "
            "delivery is the response stream of subscriptions/listen."
        ),
        "exchange": [
            {
                "step": "client -> server",
                "method": discover_body["method"],
                "headers": _redact(discover_headers),
                "body": discover_body,
                "classified_as": classify_request(discover_body, discover_headers).method,
            },
            {"step": "server -> client", "method": "server/discover", "result": discover_result},
            {"step": "negotiation", "outcome": negotiated.to_dict()},
            {
                "step": "client -> server",
                "method": read_body["method"],
                "headers": _redact(read_headers),
                "body": read_body,
            },
            {
                "step": "server -> client",
                "method": "resources/read",
                "result": {"contents": [{"uri": uri, "mimeType": "application/json",
                                         "text": json.dumps(heartbeat.to_dict(), sort_keys=True)}]},
            },
            {"step": "server -> client (stream, first frame)", "body": ack},
            {"step": "server -> client (stream)", "body": updated},
        ],
        "subscription": {
            "acknowledged_first": stream.acknowledged,
            "subscription_id": stream.subscription_id,
            "hints": [{"uri": u, "hint": h.to_dict() if h else None} for u, h in stream.hints()],
        },
    }


def fault_matrix() -> dict:
    """Drive each topology to a verdict and record it beside its rule."""
    rows = []
    for rule in TOPOLOGY_RULES:
        rows.append(
            {
                "topology": rule.topology,
                "situation": rule.situation,
                "rule": rule.rule,
                "verdict": rule.verdict.value,
                "reason": rule.reason,
            }
        )

    # An executable spot-check of the claim the whole matrix rests on: the
    # final state does not depend on how many hints were delivered.
    convergence_sweep = []
    for delivered in (0, 1, 10, 50):
        clock = FakeClock()
        issuer = HeartbeatIssuer(
            participant_id=PARTICIPANT, epoch_id="epoch-a", clock=clock, lease_seconds=30.0
        )
        documents: dict[str, dict] = {PARTICIPANT: issuer.issue().to_dict()}

        class Source:
            def fetch(self, participant_id: str) -> FetchResult:
                return FetchResult(document=documents[participant_id])

        consumer = HeartbeatConsumer(PARTICIPANT, Source(), clock)
        consumer.refetch()
        for sequence in range(1, 51):
            clock.advance(0.1)
            documents[PARTICIPANT] = issuer.issue().to_dict()
            if sequence <= delivered:
                consumer.on_hint(
                    ChangeHint(
                        address="heartbeat://participants/svc%2Fapi-7",
                        revision=f"epoch-a:{sequence}",
                        digest="sha256:" + "12" * 32,
                    )
                )
        verdict = consumer.refetch()
        convergence_sweep.append(
            {
                "hints_delivered": delivered,
                "hints_coalesced": consumer.hints_coalesced,
                "fetches": consumer.fetches,
                "final_sequence": consumer.held.sequence if consumer.held else None,
                "verdict": verdict.convergence.value,
            }
        )

    assert len({row["final_sequence"] for row in convergence_sweep}) == 1, (
        "convergence must not depend on delivery"
    )

    return {
        "artifact": "subscription-fault-matrix",
        "topologies": rows,
        "convergence_independent_of_delivery": convergence_sweep,
        "verdict_vocabulary": [c.value for c in Convergence],
    }


def provenance() -> dict:
    if not sdk.SDK_AVAILABLE:
        return {
            "artifact": "sdk-provenance",
            "available": False,
            "pinned": contract.SDK_PIN.to_dict(),
            "note": "run tools/verify_sdk.sh --evidence to regenerate against the pinned SDK",
        }
    payload = sdk.assert_contract_matches_sdk()
    payload["available"] = True
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "sdk-provenance.json": provenance(),
        "current-protocol-transcript.json": protocol_transcript(),
        "forbidden-primitive-lint.json": lint_package().to_dict(),
        "subscription-fault-matrix.json": fault_matrix(),
    }
    for name, payload in artifacts.items():
        path = args.output / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
