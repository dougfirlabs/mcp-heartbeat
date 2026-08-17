"""The publishing participant: mints a heartbeat stream and serves it.

This is one half of the two-participant demonstration. It owns a
:class:`~mcp_heartbeat.HeartbeatIssuer` and answers three routes:

``GET /heartbeat``
    The participant's current heartbeat document. While *publishing*, each
    request mints the next heartbeat in the stream. While *frozen*, the last
    minted document is returned **verbatim** — the participant is still
    reachable, still answering, and its lease is quietly lapsing. That is the
    case the whole contract exists for, and it is why the observer must never
    treat "the request succeeded" as "the node is alive".

``GET /healthz``
    Process liveness, and it says so in its own body. Deliberately useless for
    deciding anything: a green ``/healthz`` from a frozen participant is the
    demonstration's point, not a bug in it.

``POST /control``
    Scripts the scenario — freeze, thaw, restart under a new epoch, change the
    advertised status. Guarded by ``HB_DEMO_ALLOW_CONTROL`` and never exposed
    as part of the protocol. A real participant has no such endpoint; the
    demonstration needs one because it has to *cause* the situations it claims
    to observe.

Standard library only, matching the package it demonstrates. There is no web
framework here and no MCP SDK: the core is transport-neutral, so the
demonstration supplies the most boring transport that could work.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mcp_heartbeat import HeartbeatIssuer, SystemClock, mint_epoch_id

#: The vendor-namespaced extension the status ride under. Experimental, and
#: namespaced precisely so that it cannot collide with a standardised name.
STATUS_EXTENSION = "com.dougfirlabs/heartbeat"

LAYOUT = "standalone"


class Participant:
    """Issuer plus the two pieces of scenario state the control channel sets.

    ``frozen`` models a reachable-but-stuck process: the socket still answers,
    nothing new is minted. ``status`` is advisory metadata riding in the
    namespaced extension — it is *not* part of the lease judgement, and the
    observer asserts that separately.
    """

    def __init__(self, *, participant_id: str, lease_seconds: float) -> None:
        self.participant_id = participant_id
        self.lease_seconds = lease_seconds
        self.clock = SystemClock()
        self.status = "serving"
        self.frozen = False
        self.restarts = 0
        self._lock = threading.Lock()
        self._issuer = self._new_issuer()
        self._last: dict[str, Any] = {}

    def _new_issuer(self) -> HeartbeatIssuer:
        """A fresh epoch. A new process means a new epoch — so does a restart."""
        return HeartbeatIssuer(
            participant_id=self.participant_id,
            epoch_id=mint_epoch_id(),
            clock=self.clock,
            lease_seconds=self.lease_seconds,
        )

    def heartbeat(self) -> dict[str, Any]:
        """Mint the next heartbeat, or replay the last one while frozen."""
        with self._lock:
            if self.frozen and self._last:
                return dict(self._last)
            self._issuer.extensions = {STATUS_EXTENSION: {"status": self.status}}
            self._last = self._issuer.issue().to_dict()
            return dict(self._last)

    def control(self, operation: str, value: Any = None) -> dict[str, Any]:
        """Apply one scenario operation and report the resulting state."""
        with self._lock:
            if operation == "freeze":
                self.frozen = True
            elif operation == "thaw":
                self.frozen = False
            elif operation == "restart":
                # A restart mints a *new* epoch and resets the sequence. It
                # must never re-present the retired one: a consumer classifies
                # that as `boot_id_reuse`, which is the replay defence.
                self._issuer = self._new_issuer()
                self._last = {}
                self.frozen = False
                self.restarts += 1
            elif operation == "status":
                self.status = str(value)
            else:
                raise ValueError(f"unknown control operation: {operation!r}")
            return self.state()

    def state(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "epoch_id": self._issuer.epoch_id,
            "next_sequence": self._issuer.next_sequence,
            "status": self.status,
            "frozen": self.frozen,
            "restarts": self.restarts,
            "lease_seconds": self.lease_seconds,
        }


def make_handler(participant: Participant, *, allow_control: bool):
    """Bind the routes to ``participant``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.path == "/heartbeat":
                self._send(200, participant.heartbeat())
            elif self.path == "/healthz":
                # Says what it is worth in its own body, so nobody has to read
                # this file to find out that it proves almost nothing.
                self._send(
                    200,
                    {
                        "process": "alive",
                        "means": "this process is running and answering",
                        "does_not_mean": "the participant's lease is fresh — refetch /heartbeat",
                    },
                )
            elif self.path == "/state":
                self._send(200, participant.state())
            else:
                self._send(404, {"error": "no such route", "path": self.path})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/control":
                self._send(404, {"error": "no such route", "path": self.path})
                return
            if not allow_control:
                self._send(403, {"error": "control channel disabled"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, participant.control(request["op"], request.get("value")))
            except (ValueError, KeyError) as exc:
                self._send(400, {"error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            # One line per request on stderr, so `docker compose logs` is
            # readable without a logging dependency.
            print(f"[publisher] {self.address_string()} {format % args}", flush=True)

    return Handler


def main() -> None:
    participant = Participant(
        participant_id=os.environ.get("HB_DEMO_PARTICIPANT_ID", "demo/publisher-1"),
        lease_seconds=float(os.environ.get("HB_DEMO_LEASE_SECONDS", "6")),
    )
    host = os.environ.get("HB_DEMO_HOST", "0.0.0.0")
    port = int(os.environ.get("HB_DEMO_PORT", "8981"))
    allow_control = os.environ.get("HB_DEMO_ALLOW_CONTROL", "1") == "1"

    server = ThreadingHTTPServer((host, port), make_handler(participant, allow_control=allow_control))
    print(
        f"[publisher] layout: {LAYOUT} participant={participant.participant_id} "
        f"lease={participant.lease_seconds}s control={'on' if allow_control else 'off'}",
        flush=True,
    )
    server.serve_forever()


__all__ = ["LAYOUT", "STATUS_EXTENSION", "Participant", "main", "make_handler"]
