"""Per-request metadata: the modern replacement for the handshake.

Legacy MCP negotiated once and then trusted a session. 2026-07-28 carries
version, client identity and capabilities on *every* request, in ``_meta``,
mirrored by three standard headers so an intermediary can route without
parsing the body. That mirroring is only useful if a disagreement is fatal,
so the inbound path is a strict three-rung ladder and a mismatch is
``-32020``, never a best-effort reconciliation.

Nothing here mints, echoes, or reads a session identifier, and nothing here
opens a stream. Those are :data:`~mcp_heartbeat_current.contract.FORBIDDEN_PRIMITIVES`
on this path; :mod:`~mcp_heartbeat_current.lint` proves the absence
mechanically rather than by review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .contract import (
    IGNORED_LEGACY_HEADERS,
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    MODERN_PROTOCOL_VERSIONS,
    NAME_BEARING_METHODS,
    PROTOCOL_REVISION,
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)
from .errors import HeaderMismatch, MalformedEnvelope, UnsupportedProtocolVersion

#: HTTP verbs the modern MCP endpoint answers. GET and DELETE were the
#: legacy stream-open and session-terminate verbs; both are now 405.
ALLOWED_HTTP_METHODS: frozenset[str] = frozenset({"POST"})


def _lower_keys(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {str(k).lower(): v for k, v in (headers or {}).items()}


# ── outbound ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RequestEnvelope:
    """The modern per-request envelope for one JSON-RPC call.

    ``client_info`` and ``client_capabilities`` travel on every request
    because there is no session to remember them; that is not redundancy,
    it is what makes a stateless replica able to answer at all.
    """

    method: str
    params: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = PROTOCOL_REVISION
    client_info: Mapping[str, Any] = field(default_factory=dict)
    client_capabilities: Mapping[str, Any] = field(default_factory=dict)

    def meta(self) -> dict[str, Any]:
        """The ``_meta`` block this request must carry."""
        return {
            PROTOCOL_VERSION_META_KEY: self.protocol_version,
            CLIENT_INFO_META_KEY: dict(self.client_info),
            CLIENT_CAPABILITIES_META_KEY: dict(self.client_capabilities),
        }

    def headers(self) -> dict[str, str]:
        """The standard headers mirroring this request.

        ``Mcp-Name`` appears only for the methods that define it. Emitting it
        elsewhere would make the header meaningless, and omitting it for
        ``resources/read`` — the heartbeat fetch — is non-conformant.
        """
        headers = {
            MCP_PROTOCOL_VERSION_HEADER: self.protocol_version,
            MCP_METHOD_HEADER: self.method,
        }
        name_param = NAME_BEARING_METHODS.get(self.method)
        if name_param is not None:
            value = self.params.get(name_param)
            if value is None:
                raise MalformedEnvelope(
                    f"{self.method} requires params[{name_param!r}] for {MCP_NAME_HEADER}"
                )
            headers[MCP_NAME_HEADER] = str(value)
        return headers

    def body(self, request_id: str | int) -> dict[str, Any]:
        """The full JSON-RPC request object, envelope included."""
        params = dict(self.params)
        params["_meta"] = {**dict(params.get("_meta") or {}), **self.meta()}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": self.method,
            "params": params,
        }


def build_request(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    request_id: str | int,
    protocol_version: str = PROTOCOL_REVISION,
    client_info: Mapping[str, Any] | None = None,
    client_capabilities: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(body, headers)`` for one conformant modern request."""
    envelope = RequestEnvelope(
        method=method,
        params=params or {},
        protocol_version=protocol_version,
        client_info=client_info or {},
        client_capabilities=client_capabilities or {},
    )
    return envelope.body(request_id), envelope.headers()


# ── inbound ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModernRoute:
    """A request that cleared all three rungs.

    ``ignored_headers`` records legacy headers that arrived and were
    dropped. Recording rather than silently discarding is what makes a
    gateway that still injects a session id *visible* instead of merely
    harmless.
    """

    protocol_version: str
    method: str
    client_info: Mapping[str, Any]
    client_capabilities: Mapping[str, Any]
    ignored_headers: tuple[str, ...] = ()


def classify_request(
    body: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    *,
    supported_versions: tuple[str, ...] = MODERN_PROTOCOL_VERSIONS,
) -> ModernRoute:
    """Validate one inbound modern request, or raise the ladder's error.

    The rungs run in this order and the order is normative:

    1. **Envelope** — ``_meta`` carries protocol version and client
       capabilities, else ``-32602``. Checked first because the later rungs
       compare *against* the envelope, and comparing against a missing value
       would report a header mismatch for a body that never declared one.
    2. **Headers** — version, method and name mirror the body exactly, else
       ``-32020``. Exact string equality: a header that is "close enough"
       is a routing decision made on a value the server did not verify.
    3. **Version** — the declared revision is one this server speaks, else
       ``-32022`` naming what is supported.
    """
    lowered = _lower_keys(headers)
    method = body.get("method")
    if not isinstance(method, str) or not method:
        raise MalformedEnvelope("request is missing a JSON-RPC method")

    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    if not isinstance(meta, Mapping):
        raise MalformedEnvelope(f"{method} is missing the modern _meta envelope")

    version = meta.get(PROTOCOL_VERSION_META_KEY)
    if not isinstance(version, str) or not version:
        raise MalformedEnvelope(f"{method} is missing _meta[{PROTOCOL_VERSION_META_KEY!r}]")
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, Mapping):
        raise MalformedEnvelope(f"{method} is missing _meta[{CLIENT_CAPABILITIES_META_KEY!r}]")

    header_version = lowered.get(MCP_PROTOCOL_VERSION_HEADER)
    if header_version is not None and header_version != version:
        raise HeaderMismatch(
            f"{MCP_PROTOCOL_VERSION_HEADER} {header_version!r} != _meta protocol version {version!r}"
        )
    header_method = lowered.get(MCP_METHOD_HEADER)
    if header_method is not None and header_method != method:
        raise HeaderMismatch(f"{MCP_METHOD_HEADER} {header_method!r} != method {method!r}")

    name_param = NAME_BEARING_METHODS.get(method)
    if name_param is not None:
        header_name = lowered.get(MCP_NAME_HEADER)
        body_name = params.get(name_param) if isinstance(params, Mapping) else None
        if header_name is not None and body_name is not None and header_name != str(body_name):
            raise HeaderMismatch(
                f"{MCP_NAME_HEADER} {header_name!r} != params[{name_param!r}] {body_name!r}"
            )

    if version not in supported_versions:
        raise UnsupportedProtocolVersion(version, supported_versions)

    ignored = tuple(h for h in IGNORED_LEGACY_HEADERS if h in lowered)
    client_info = meta.get(CLIENT_INFO_META_KEY)
    return ModernRoute(
        protocol_version=version,
        method=method,
        client_info=client_info if isinstance(client_info, Mapping) else {},
        client_capabilities=capabilities,
        ignored_headers=ignored,
    )


def response_headers(route: ModernRoute) -> dict[str, str]:
    """Headers a modern response carries back.

    Notably short: the revision, and nothing that could be mistaken for a
    session handle. A legacy session id that arrived on the request is
    *not* echoed — see :attr:`ModernRoute.ignored_headers`.
    """
    return {MCP_PROTOCOL_VERSION_HEADER: route.protocol_version}


# ── transport-level guards ────────────────────────────────────────────


def origin_allowed(origin: str | None, allowed_origins: Iterable[str]) -> bool:
    """Whether a browser ``Origin`` may reach this endpoint.

    Absent is allowed — a non-browser client sends no Origin, and rejecting
    it would break every server-to-server deployment. Present-and-unknown is
    refused with 403, which is the DNS-rebinding defence.
    """
    if origin is None:
        return True
    return origin in set(allowed_origins)


def http_method_allowed(http_method: str) -> bool:
    """Whether the modern MCP endpoint answers ``http_method``.

    ``GET`` was the standalone notification stream and ``DELETE`` was
    session termination. Both were removed; both are 405 here, which is what
    tells a legacy client it is talking to a modern-only endpoint instead of
    leaving it hanging on a stream that will never emit.
    """
    return http_method.upper() in ALLOWED_HTTP_METHODS


__all__ = [
    "ALLOWED_HTTP_METHODS",
    "ModernRoute",
    "RequestEnvelope",
    "build_request",
    "classify_request",
    "http_method_allowed",
    "origin_allowed",
    "response_headers",
]
