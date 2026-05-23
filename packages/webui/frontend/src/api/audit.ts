import { apiClient } from "./client";

/**
 * Audit outcome — superset of what the router emits today (`ok`,
 * `rate-limited`, `ignored`) plus the variants other code paths use
 * (`err`, `error`). Typed as a union of known literals plus
 * ``string`` so we can still display unknown values without a build
 * break when the backend introduces a new bucket.
 */
export type AuditOutcome =
  | "ok"
  | "err"
  | "error"
  | "rate-limited"
  | "ignored"
  | (string & {}); // catch-all that preserves IDE autocomplete

export interface AuditEntry {
  id: string;
  time: string;
  bot_id: string;
  user_id: string;
  scope_id: string;
  kind: string;
  outcome: AuditOutcome;
  latency_ms: number | null;
  payload: Record<string, unknown>;
}

export interface AuditSearchParams {
  bot_id?: string;
  user_id?: string;
  kind?: string;
  outcome?: AuditOutcome;
  q?: string;
  limit?: number;
}

export async function searchAudit(params: AuditSearchParams = {}): Promise<AuditEntry[]> {
  const r = await apiClient.get<AuditEntry[]>("/audit", { params });
  return r.data;
}

export function csvExportUrl(params: AuditSearchParams = {}): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  return `/api/audit.csv?${q.toString()}`;
}
