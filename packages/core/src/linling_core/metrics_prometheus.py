"""Prometheus backend for :class:`MetricsSink`.

Imported lazily — ``import linling_core.metrics_prometheus`` requires
``prometheus_client`` to be installed. Deployments that don't need
Prometheus never import this module and thus never pay that cost.

The backend keeps its own :class:`CollectorRegistry` so tests can build
and dispose instances without mutating the global registry (which
:mod:`prometheus_client` shares between imports).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

from linling_core.metrics import (
    ACTIVE_SESSIONS,
    DISPATCH_DURATION_SECONDS,
    LLM_CALLS_TOTAL,
    LLM_DURATION_SECONDS,
    LLM_TOKENS_TOTAL,
    ROUTER_DUPLICATES_TOTAL,
    ROUTER_EVENTS_TOTAL,
    SINK_FAILURES_TOTAL,
    Labels,
)

if TYPE_CHECKING:
    pass


# Histogram buckets tuned for bot dispatch: mostly sub-second, with a
# long tail capped at 30s (router's session timeout).
_DISPATCH_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

# LLM calls are generally in the 0.3 - 20 s range.
_LLM_BUCKETS = (
    0.1,
    0.25,
    0.5,
    1.0,
    2.0,
    4.0,
    8.0,
    15.0,
    30.0,
    60.0,
    120.0,
)


class PrometheusMetrics:
    """A :class:`MetricsSink` backed by an isolated Prometheus registry.

    Construct one per process; pass the same instance to ``set_metrics``
    (for process-default recording) and to the WebUI's ``/metrics``
    endpoint (for scraping).
    """

    def __init__(self) -> None:
        self._registry = CollectorRegistry()

        # -- counters ----------------------------------------------
        self._events_total = Counter(
            ROUTER_EVENTS_TOTAL,
            "Total events classified by the router.",
            ["bot_id", "platform", "kind", "outcome"],
            registry=self._registry,
        )
        self._duplicates_total = Counter(
            ROUTER_DUPLICATES_TOTAL,
            "Events dropped because the same id was already processed.",
            ["bot_id"],
            registry=self._registry,
        )
        self._sink_failures_total = Counter(
            SINK_FAILURES_TOTAL,
            "Outbound action deliveries that failed.",
            ["bot_id", "platform"],
            registry=self._registry,
        )
        self._llm_tokens_total = Counter(
            LLM_TOKENS_TOTAL,
            "Total LLM tokens (prompt + completion).",
            ["provider", "model", "direction"],
            registry=self._registry,
        )
        self._llm_calls_total = Counter(
            LLM_CALLS_TOTAL,
            "Total LLM calls.",
            ["provider", "model", "outcome"],
            registry=self._registry,
        )

        # -- histograms --------------------------------------------
        self._dispatch_duration = Histogram(
            DISPATCH_DURATION_SECONDS,
            "Router end-to-end dispatch latency.",
            ["bot_id", "kind"],
            buckets=_DISPATCH_BUCKETS,
            registry=self._registry,
        )
        self._llm_duration = Histogram(
            LLM_DURATION_SECONDS,
            "LLM call round-trip latency.",
            ["provider", "model"],
            buckets=_LLM_BUCKETS,
            registry=self._registry,
        )

        # -- gauges ------------------------------------------------
        self._active_sessions = Gauge(
            ACTIVE_SESSIONS,
            "Active conversation sessions in the ConversationStore.",
            ["bot_id"],
            registry=self._registry,
        )

        # Dispatch table keyed by metric name. Wrapped ``Counter`` /
        # ``Histogram`` / ``Gauge`` instances are stored with a tuple
        # of required label names so ``MetricsSink`` calls with an
        # incomplete label set can raise cleanly instead of producing
        # silently-wrong series.
        self._counters: dict[str, tuple[Counter, tuple[str, ...]]] = {
            ROUTER_EVENTS_TOTAL: (self._events_total, ("bot_id", "platform", "kind", "outcome")),
            ROUTER_DUPLICATES_TOTAL: (self._duplicates_total, ("bot_id",)),
            SINK_FAILURES_TOTAL: (self._sink_failures_total, ("bot_id", "platform")),
            LLM_TOKENS_TOTAL: (self._llm_tokens_total, ("provider", "model", "direction")),
            LLM_CALLS_TOTAL: (self._llm_calls_total, ("provider", "model", "outcome")),
        }
        self._histograms: dict[str, tuple[Histogram, tuple[str, ...]]] = {
            DISPATCH_DURATION_SECONDS: (self._dispatch_duration, ("bot_id", "kind")),
            LLM_DURATION_SECONDS: (self._llm_duration, ("provider", "model")),
        }
        self._gauges: dict[str, tuple[Gauge, tuple[str, ...]]] = {
            ACTIVE_SESSIONS: (self._active_sessions, ("bot_id",)),
        }

    # -- MetricsSink protocol --------------------------------------

    def counter_inc(self, name: str, labels: Labels, amount: float = 1.0) -> None:
        entry = self._counters.get(name)
        if entry is None:
            return
        counter, required = entry
        counter.labels(**_ordered(required, labels)).inc(amount)

    def histogram_observe(self, name: str, labels: Labels, value: float) -> None:
        entry = self._histograms.get(name)
        if entry is None:
            return
        hist, required = entry
        hist.labels(**_ordered(required, labels)).observe(value)

    def gauge_set(self, name: str, labels: Labels, value: float) -> None:
        entry = self._gauges.get(name)
        if entry is None:
            return
        gauge, required = entry
        gauge.labels(**_ordered(required, labels)).set(value)

    # -- scrape surface --------------------------------------------

    def render(self) -> tuple[bytes, str]:
        """Return ``(body, content_type)`` suitable for an HTTP response."""
        return _generate_latest(self._registry), CONTENT_TYPE_LATEST

    @property
    def registry(self) -> CollectorRegistry:
        """Expose the underlying registry (handy for custom collectors)."""
        return self._registry


def _ordered(required: tuple[str, ...], labels: Labels) -> dict[str, str]:
    """Pick exactly the labels the metric expects, raise on mismatch.

    Missing labels default to ``"unknown"`` — a series is still
    produced, which is better than silently losing the observation.
    Extra labels are ignored. This keeps callers ergonomic (they pass
    whatever they have) without polluting cardinality.
    """
    return {key: str(labels.get(key, "unknown")) for key in required}
