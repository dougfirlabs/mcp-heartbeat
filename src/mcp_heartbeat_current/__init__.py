"""MCP Heartbeat — the current-era (MCP 2026-07-28) adapter.

A greenfield binding, not a migration. There was no modern surface in this
tree to port (HB-00 established that), and reusing legacy lifecycle
mechanics on a new path would have produced something internally coherent
and non-conformant — the exact failure this adapter exists to avoid.

What the modern era changes, and therefore what this package is:

* **No handshake.** ``server/discover`` returns supported revisions,
  capabilities and identity in one request — :mod:`.discovery`.
* **Per-request metadata.** Version, client identity and capabilities ride
  in ``_meta`` on every call, mirrored by three standard headers that must
  match exactly — :mod:`.metadata`.
* **No standalone notification stream.** Change hints arrive on the
  response stream of a ``subscriptions/listen`` request, after an
  acknowledgement — :mod:`.subscriptions`.
* **Prefixed extension identifiers.** Heartbeat is advertised as
  ``com.dougfirlabs/heartbeat`` — one constant, in :mod:`.contract`.

Two things it deliberately does *not* do. It never lets a change hint
decide anything: the authoritative ``resources/read`` and the portable
core's ``admit()`` do, which is why every delivery fault in
:mod:`.convergence` is a latency question rather than a correctness one.
And it never binds identity per channel: :mod:`.identity` computes the
``identity_binding`` facet per response from that request's authenticated
principal, closing HB-00 defect D-05.

The layering is the same one the portable core uses, one level up. Only
:mod:`.sdk` imports the official SDK; everything else is standard library
plus ``mcp_heartbeat``, so the adapter's logic is testable — and the
package installable — with no MCP SDK present. :mod:`.lint` proves that
mechanically, along with the absence of every forbidden legacy primitive.
"""
from __future__ import annotations

from . import (
    contract,
    convergence,
    discovery,
    era,
    errors,
    identity,
    lint,
    metadata,
    subscriptions,
)
from .contract import *  # noqa: F403
from .convergence import *  # noqa: F403
from .discovery import *  # noqa: F403
from .era import *  # noqa: F403
from .errors import *  # noqa: F403
from .identity import *  # noqa: F403
from .lint import *  # noqa: F403
from .metadata import *  # noqa: F403
from .subscriptions import *  # noqa: F403

#: Versions the adapter binding itself. Distinct from both the MCP protocol
#: revision and the heartbeat extension version — three axes, three numbers.
__version__ = "0.1.0"

# ``sdk`` is deliberately absent from the star-imports and from the eager
# import list: importing it is harmless without the SDK, but re-exporting
# its names would make ``from mcp_heartbeat_current import *`` behave
# differently depending on what happens to be installed.

__all__ = ["__version__", "sdk"] + sorted(
    name
    for module in (
        contract,
        convergence,
        discovery,
        era,
        errors,
        identity,
        lint,
        metadata,
        subscriptions,
    )
    for name in module.__all__
)
