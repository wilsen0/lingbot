import { apiClient } from "./client";

export interface AgentSummary {
  name: string;
  provider: string;
  model: string;
  token_today?: number;
}

export async function listAgents(): Promise<AgentSummary[]> {
  const r = await apiClient.get<AgentSummary[]>("/agents");
  return r.data;
}

export async function getAgent(name: string): Promise<AgentSummary> {
  const r = await apiClient.get<AgentSummary>(`/agents/${encodeURIComponent(name)}`);
  return r.data;
}

export interface MemoryMessage {
  role: string;
  content: string;
  name?: string | null;
}
export interface MemoryView {
  short_term: MemoryMessage[];
  summary: string;
  long_term: unknown[];
}

export async function getAgentMemory(
  name: string,
  userId?: string,
  scopeId = "test",
): Promise<MemoryView> {
  const params: { user_id?: string; scope_id: string } = { scope_id: scopeId };
  if (userId !== undefined) params.user_id = userId;
  const r = await apiClient.get<MemoryView>(`/agents/${encodeURIComponent(name)}/memory`, {
    params,
  });
  return r.data;
}

export interface ChatResponse {
  content: string;
  tool_calls_made: number;
  total_tokens: number;
  latency_ms: number;
}

export async function chatOnce(name: string, input: string): Promise<ChatResponse> {
  const r = await apiClient.post<ChatResponse>(`/agents/${encodeURIComponent(name)}/chat`, {
    input,
  });
  return r.data;
}

/**
 * One DSL trigger surfaced to the chat composer's inline-suggest panel.
 *
 * - ``raw`` is the bot's source-of-truth trigger (regex/literal); the
 *   UI never displays this — it's there so a future power-user mode
 *   can show the underlying pattern.
 * - ``label`` is the cleaned-up display form: ``([0-9]+)`` becomes
 *   ``…``. Render this directly.
 * - ``has_args`` flags triggers that need user input after a literal
 *   prefix; the composer parks the cursor at the placeholder rather
 *   than auto-sending.
 * - ``literal_prefix`` is the chunk before the first ``…``; used for
 *   prefix-match scoring and as the pre-fill on click.
 */
export interface TriggerSuggestion {
  raw: string;
  label: string;
  has_args: boolean;
  literal_prefix: string;
}

/** Fetch the agent's bot's matchable DSL triggers.
 *
 * Returns ``[]`` when the agent isn't backed by a bot (test harness,
 * agent-only deployments) — the composer panel hides itself. Errors
 * are surfaced; callers should swallow them so a flaky network doesn't
 * blow up the chat input. */
export async function listTriggers(name: string): Promise<TriggerSuggestion[]> {
  const r = await apiClient.get<TriggerSuggestion[]>(
    `/agents/${encodeURIComponent(name)}/triggers`,
  );
  return r.data;
}
