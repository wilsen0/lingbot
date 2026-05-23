import { apiClient } from "./client";

export interface RuleSummary {
  name: string;
  trigger: string;
  hits_today: number;
  avg_latency_ms: number;
  last_error: string | null;
}

export async function listRules(): Promise<RuleSummary[]> {
  const r = await apiClient.get<RuleSummary[]>("/rules");
  return r.data;
}

export interface RuleHit {
  id: string;
  /** ISO-8601 timestamp; the server emits :class:`AuditRow.time` as an
   *  epoch-second ``float`` via ``linling_webui.routers.rules.hits_for``.
   *  Keep the field typed as ``number | string`` so a future switch to
   *  ISO (to match ``/api/audit`` and ``/api/events``) is non-breaking.
   */
  time: number | string;
  bot_id: string;
  user_id: string;
  scope_id: string;
  outcome: "ok" | "err";
  latency_ms: number | null;
  matched: Record<string, string>;
  event_id: string | null;
}

export async function getRuleHits(name: string, limit = 50): Promise<RuleHit[]> {
  const r = await apiClient.get<RuleHit[]>(`/rules/${encodeURIComponent(name)}/hits`, {
    params: { limit },
  });
  return r.data;
}
