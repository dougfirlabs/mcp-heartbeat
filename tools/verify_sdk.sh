#!/usr/bin/env bash
#
# Run the current adapter's SDK conformance suite against the pinned
# official MCP Python SDK, in an environment of its own.
#
# Why not the calling virtualenv: installing `mcp==2.0.0` there resolves
# `pydantic-core` to a version other than the one the installed `pydantic`
# pins. That would mutate an environment the whole repository depends on,
# for an adapter that does not need to live in it. So the SDK gets a
# throwaway venv, and the rest of the package stays testable without it.
#
# Usage:
#   tools/verify_sdk.sh [--evidence]
#
# With --evidence, regenerates docs/evidence/mcp-heartbeat-hb03/.
#
# On a passing run this also writes docs/sdk-verification.json — the
# attestation that lets the cross-era matrix report the SDK leg as
# exercised from an environment that cannot import the SDK. See
# mcp_heartbeat_conformance.sdk_attestation for why that is a transcript
# rather than a reclassification, and what makes it lapse.
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${MCP_HEARTBEAT_SDK_VENV:-${PACKAGE_ROOT}/.venv-mcp-sdk}"

MCP_VERSION="2.0.0"
MCP_TYPES_VERSION="2.0.0"

if [[ ! -x "${VENV}/bin/python" ]]; then
    echo "creating isolated SDK venv at ${VENV}"
    python3 -m venv "${VENV}"
fi

# PYTHONPATH is cleared throughout: the ambient value on a dev box points at
# a parent repository's src/, and an SDK conformance run that could import it
# would not be proving what it claims to.
env -u PYTHONPATH "${VENV}/bin/pip" install --quiet --upgrade \
    "mcp==${MCP_VERSION}" "mcp-types==${MCP_TYPES_VERSION}" pytest pytest-timeout

echo "--- installed ---"
env -u PYTHONPATH "${VENV}/bin/python" - <<'PY'
import importlib.metadata as md
for dist in ("mcp", "mcp-types"):
    print(f"{dist}=={md.version(dist)}")
PY

echo "--- conformance suite ---"
cd "${PACKAGE_ROOT}"

# `set -e` would abort before STATUS could be read, so the failure is
# caught explicitly: a red suite must still reach the recorder below,
# which is what refuses to write an attestation for it.
JUNIT="$(mktemp -t hb-sdk-junit-XXXXXX.xml)"
trap 'rm -f "${JUNIT}"' EXIT

STATUS=0
env -u PYTHONPATH "${VENV}/bin/python" -m pytest tests/current/ -q --timeout=60 \
    --junit-xml="${JUNIT}" || STATUS=$?

echo "--- recording the run ---"
env -u PYTHONPATH "${VENV}/bin/python" tools/record_sdk_verification.py \
    --junit-xml "${JUNIT}" \
    --recorded-by "tools/verify_sdk.sh" \
    --recorded-on "$(date -u +%Y-%m-%d)" || true

if [[ "${1:-}" == "--evidence" ]]; then
    echo "--- regenerating evidence ---"
    env -u PYTHONPATH "${VENV}/bin/python" tools/emit_evidence.py \
        --output "${PACKAGE_ROOT}/docs/evidence/mcp-heartbeat-hb03"
fi

exit "${STATUS}"
