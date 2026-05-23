"""Metrics abstraction.

The router, agent runtime, and conversation store all emit numeric
observations — events per kind, dispatch latency, LLM tokens, active
session count. The interface here lets the rest of the codebase record
those without committing to a metrics backend.

Two concrete implementations ship:

* :class:`NullMetrics` — discard. Default. Zero allocation on the hot
  path aside from a handful of empty method calls.
* :class:`PrometheusMetrics` (in :mod:`linling_core.metrics_prometheus`) —
  wraps ``prometheus_client`` ``Counter`` / ``Histogram`` / ``Gauge``.
  Only imported when an operator opts in, so ``prometheus_client`` can
  stay an optional dependency.

Labels must be low-cardinality. User IDs, trace IDs, and anything
else with more than a few hundred distinct values belong in logs
(structlog) and audit rows, not in Prometheus series.

Naming follows the Prometheus conventions:

* Counters end in ``_total``.
* Histograms end in ``_seconds`` for durations and ``_bytes`` for sizes.
* Gauges are plain nouns.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

Labels = Mapping[str, str]


@runtime_checkable
class MetricsSink(Protocol):
    """Minimal metrics recording surface.

    Every call is expected to be O(1) and non-blocking. Implementations
    that need to buffer or ship data asynchronously do so behind a
    queue; the caller doesn't wait.
    """

    def counter_inc(self, name: str, labels: Labels, amount: float = 1.0) -> None:
        """Increment a named counter by ``amount``."""

    def histogram_observe(self, name: str, labels: Labels, value: float) -> None:
        """Record a single observation into a histogram."""

    def gauge_set(self, name: str, labels: Labels, value: float) -> None:
        """Set the current value of a gauge."""


class NullMetrics:
    """Default sink — all operations are no-ops."""

    def counter_inc(self, name: str, labels: Labels, amount: float = 1.0) -> None:
        return None

    def histogram_observe(self, name: str, labels: Labels, value: float) -> None:
        return None

    def gauge_set(self, name: str, labels: Labels, value: float) -> None:
        return None


# A process-wide default so modules that don't accept an explicit sink
# (e.g. pure utility code) can still record observations if the bot
# swapped the default at startup. Treat this as operator-settable
# configuration, not hidden state: ``bootstrap_bot`` decides the value.
_default_metrics: MetricsSink = NullMetrics()


def get_metrics() -> MetricsSink:
    """Return the process-default metrics sink."""
    return _default_metrics


def set_metrics(sink: MetricsSink) -> None:
    """Replace the process-default metrics sink.

    Called by the bootstrap when an operator enables Prometheus. The
    assignment is atomic in CPython; in-flight observations pass
    through whichever sink they captured first, which is fine (a brief
    double-write or dropped-write window is acceptable).
    """
    global _default_metrics  # noqa: PLW0603 — the setter pattern requires module state.
    _default_metrics = sink


# ---------------------------------------------------------------------------
# Metric name constants — single source of truth used by callers & Prom definitions.
# ---------------------------------------------------------------------------

# Counters
ROUTER_EVENTS_TOTAL = "linling_events_total"
"""Labels: ``bot_id``, ``platform``, ``kind``, ``outcome``."""

ROUTER_DUPLICATES_TOTAL = "linling_router_duplicates_total"
"""Labels: ``bot_id``."""

SINK_FAILURES_TOTAL = "linling_sink_failures_total"
"""Labels: ``bot_id``, ``platform``."""

LLM_TOKENS_TOTAL = "linling_llm_tokens_total"
"""Labels: ``provider``, ``model``, ``direction`` (``prompt``/``completion``)."""

LLM_CALLS_TOTAL = "linling_llm_calls_total"
"""Labels: ``provider``, ``model``, ``outcome`` (``ok``/``error``)."""

# Histograms
DISPATCH_DURATION_SECONDS = "linling_dispatch_duration_seconds"
"""Labels: ``bot_id``, ``kind``."""

LLM_DURATION_SECONDS = "linling_llm_duration_seconds"
"""Labels: ``provider``, ``model``."""

# Gauges
ACTIVE_SESSIONS = "linling_active_sessions"
"""Labels: ``bot_id``. Snapshot of :class:`ConversationStore` size."""
