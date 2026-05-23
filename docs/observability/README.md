# Observability

linling bots emit three complementary signals:

- **Structured logs** via `structlog`. Each dispatch gets a `trace_id`; every log
  line within that dispatch is stamped with the id (plus `bot_id` and
  `event_id`). Search your log store with `trace_id="abc123"` to see one
  user message's full journey.

- **Audit records** via `/api/audit` (and `/ws/rules/hits`). One row per
  routed event: `kind`, `outcome`, `latency_ms`, `verdict`, and the
  `trace_id`. The WebUI renders these for operator self-service.

- **Prometheus metrics** at `GET /metrics` (when `metrics.enabled=true`
  in `bot.yaml`). This directory holds the scrape config and Grafana
  dashboard for that endpoint.

## Metric catalog

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `linling_events_total` | counter | `bot_id, platform, kind, outcome` | Every event the router resolved. `kind` is `command`/`chat`/`help`/`reset`/`cancel`/`ignore`/`unknown-command`/`backpressure`. `outcome` is `ok`/`rate-limited`/`ignored`/`error`. |
| `linling_router_duplicates_total` | counter | `bot_id` | Events dropped because the same `id` was already processed. Adapter retries normally; spikes indicate a flapping upstream. |
| `linling_sink_failures_total` | counter | `bot_id, platform` | Outbound `Action` deliveries that raised. |
| `linling_llm_calls_total` | counter | `provider, model, outcome` | LLM chat invocations. `outcome` is `ok`/`error`. |
| `linling_llm_tokens_total` | counter | `provider, model, direction` | Token usage. `direction` is `prompt`/`completion`. |
| `linling_dispatch_duration_seconds` | histogram | `bot_id, kind` | Wall-clock router end-to-end latency. |
| `linling_llm_duration_seconds` | histogram | `provider, model` | LLM round-trip latency. |
| `linling_active_sessions` | gauge | `bot_id` | Snapshot of `ConversationStore` size. |

## Cardinality

Labels are deliberately low-cardinality. User and scope identifiers
never enter Prometheus — they live in logs (keyed by `trace_id`) and
audit rows. That keeps series count bounded: `O(bots × platforms × kinds
× outcomes)` plus similar products for the LLM metrics.

## Files

- [`prometheus.yml`](./prometheus.yml) — minimal scrape config for a
  local Prometheus targeting `127.0.0.1:8787`.
- [`alerts.yml`](./alerts.yml) — PromQL alert rules for the common
  SLO-breaking conditions (error burn, latency spikes, LLM errors,
  sink failures, stale bots).
- [`grafana-dashboard.json`](./grafana-dashboard.json) — import-ready
  dashboard with per-bot panels. Uses the default Prometheus datasource
  variable `$DS_PROMETHEUS`.

## Local quickstart

```sh
# 1. Start the bot with metrics turned on (already done in bot/bot.yaml).
uv run linling run bot/bot.yaml --webui

# 2. Scrape it.
prometheus --config.file=docs/observability/prometheus.yml

# 3. Run Grafana (any way you like), add a Prometheus datasource pointing
#    at 127.0.0.1:9090, and import docs/observability/grafana-dashboard.json.
```
