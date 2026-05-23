"""Audit sink abstraction.

The router emits one :class:`AuditEntry` per handled event so that
operators can answer "why did this message go to the agent / this
handler / nowhere?" without having to re-parse structlog output. The
sink is an injection seam:

* Default: ``NullAuditSink`` — discard, zero overhead.
* Production: the WebUI wires in a sink that forwards to
  :class:`linling_webui.audit_reader.AuditReader`, which the
  ``/api/audit`` endpoint and the live ``/ws/rules/hits`` feed already
  consume.
* Future: a persistent store (Postgres, BigQuery, …) can plug in here
  without touching the router.

Only core types appear on the interface, so downstream packages can
implement their own sink without cyclic imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class AuditEntry:
    """One row of router dispatch history.

    Fields match :class:`linling_webui.audit_reader.AuditRow` 1:1 so a
    sink forwarding to the WebUI can pass through unchanged. The
    ``trace_id`` column lets logs (which structlog stamps via
    :func:`bind_trace_id`) join cleanly to audit rows.
    """

    trace_id: str
    bot_id: str
    scope_id: str
    user_id: str
    kind: str  # "command" | "chat" | "help" | "reset" | "unknown-command" | "ignore" | ...
    outcome: str  # "ok" | "rate-limited" | "backpressure" | "error" | ...
    verdict: str  # human-readable router verdict string (``router.dispatched``)
    latency_ms: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AuditSink(Protocol):
    """Write a single audit entry. Must not raise on best-effort paths.

    The router catches exceptions around sink calls, but a well-behaved
    sink should fail closed (drop the entry + log) rather than block or
    throw.
    """

    def write(self, entry: AuditEntry) -> None: ...


class NullAuditSink:
    """Default audit sink — discards everything.

    Used when no observer (e.g. WebUI) is attached. Selecting a concrete
    sink is a deployment-time decision made by the bootstrap.
    """

    def write(self, entry: AuditEntry) -> None:
        """Drop the entry."""
        return None
