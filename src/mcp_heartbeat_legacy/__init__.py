"""The legacy MCP adapter for the portable heartbeat core.

Binds :mod:`mcp_heartbeat` to MCP revision ``2025-06-18`` (and ``2025-03-26``)
lifecycle, capability, resource, and notification mechanics. It is a *sibling*
of the core, never part of it: the core stays stdlib-only and era-free, and
this package is the only place a legacy method name appears.

It exists for compatibility, not as a model. Legacy mechanics are explicitly
non-normative for new implementations, and the deprecation policy is in
``docs/legacy-compatibility.md``.

Four HB-00 defects are repaired here, each with positive and negative coverage
in the separately versioned corpus under ``tests/legacy/corpus/``:

===== ============================================ =================
id    defect                                       repaired in
===== ============================================ =================
D-02  ``resources.subscribe`` advertised, unserved :mod:`.capabilities`
D-03  ``notifications/initialized`` never exchanged :mod:`.session`
D-04  any ``protocolVersion`` echoed, unvalidated  :mod:`.era`
D-10  revision metadata overloaded onto the        :mod:`.resources`
      standard updated-notification
===== ============================================ =================

The archived HB-00 characterization tests are untouched: they pin the
*historical* implementation in the originating integration lab, which this
package does not modify. Historical and repaired corpora are independently
runnable, which is the point.

    >>> from mcp_heartbeat_legacy import LegacyClientSession, LegacyServerSession
    >>> server = LegacyServerSession(server_name="lab", implemented={"resources/read"})
    >>> client = LegacyClientSession(client_name="probe")
    >>> _, params = client.initialize_request()
    >>> report = client.consume_initialize_result(server.handle("initialize", params))
    >>> server.handle(*client.initialized_notification())
    >>> (report.mcp_protocol_era, report.extension_version, report.heartbeat_supported)
    ('2025-06-18', '0.1', True)
"""
from __future__ import annotations

from . import capabilities, era, errors, identity, resources, session
from .capabilities import *  # noqa: F403
from .era import *  # noqa: F403
from .errors import *  # noqa: F403
from .identity import *  # noqa: F403
from .resources import *  # noqa: F403
from .session import *  # noqa: F403

#: The adapter's own version. Distinct from both the MCP protocol era and the
#: heartbeat ``extension_version`` — three axes, none of them each other.
__version__ = "0.1.0"

__all__ = ["__version__"] + sorted(
    name
    for module in (capabilities, era, errors, identity, resources, session)
    for name in module.__all__
)
