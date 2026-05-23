import { apiClient } from "./client";

export interface KvNamespace {
  scope: string;
  file: string;
  count: number;
}

export interface KvRow {
  bot_id: string;
  scope: string;
  file: string;
  key: string;
  value: string;
  updated_at: number;
}

export interface KvPage {
  items: KvRow[];
  next_cursor: string | null;
}

/**
 * List all (scope, file) pairs — the "阁中玉" directory.
 * If ``scope`` is omitted, the server enumerates every scope it knows about.
 */
export async function listNamespaces(params: { bot_id?: string; scope?: string } = {}): Promise<
  KvNamespace[]
> {
  const r = await apiClient.get<KvNamespace[]>("/kv", { params });
  return r.data;
}

export async function listKvKeys(params: {
  scope: string;
  file: string;
  bot_id?: string;
  prefix?: string;
  cursor?: string;
  limit?: number;
}): Promise<KvPage> {
  const { scope, file, ...rest } = params;
  const r = await apiClient.get<KvPage>(
    `/kv/${encodeURIComponent(scope)}/${encodeURIComponent(file)}`,
    { params: rest },
  );
  return r.data;
}

export async function readKey(params: {
  scope: string;
  file: string;
  key: string;
  bot_id?: string;
}): Promise<{ row: KvRow; etag: string | null }> {
  const r = await apiClient.get<KvRow>(
    `/kv/${encodeURIComponent(params.scope)}/${encodeURIComponent(params.file)}/${encodeURIComponent(params.key)}`,
    { params: params.bot_id ? { bot_id: params.bot_id } : {} },
  );
  return { row: r.data, etag: r.headers.etag ?? null };
}

export async function writeKey(params: {
  scope: string;
  file: string;
  key: string;
  value: string;
  bot_id?: string;
  ifMatch?: string | null;
}): Promise<{ row: KvRow; etag: string | null }> {
  const r = await apiClient.patch<KvRow>(
    `/kv/${encodeURIComponent(params.scope)}/${encodeURIComponent(params.file)}/${encodeURIComponent(params.key)}`,
    { value: params.value },
    {
      params: params.bot_id ? { bot_id: params.bot_id } : {},
      headers: params.ifMatch ? { "If-Match": params.ifMatch } : undefined,
    },
  );
  return { row: r.data, etag: r.headers.etag ?? null };
}

export async function deleteKey(params: {
  scope: string;
  file: string;
  key: string;
  bot_id?: string;
}): Promise<void> {
  await apiClient.delete(
    `/kv/${encodeURIComponent(params.scope)}/${encodeURIComponent(params.file)}/${encodeURIComponent(params.key)}`,
    { params: params.bot_id ? { bot_id: params.bot_id } : {} },
  );
}

export interface KvRankResponse {
  rows: Array<{ rank: number; key: string; value: string; numeric: number }>;
  formatted: string;
}

export async function rankKv(params: {
  scope: string;
  file: string;
  bot_id?: string;
  order?: "asc" | "desc";
  top?: number;
  sep?: string;
  fmt?: string;
}): Promise<KvRankResponse> {
  const { scope, file, ...rest } = params;
  const r = await apiClient.get<KvRankResponse>(
    `/kv/${encodeURIComponent(scope)}/${encodeURIComponent(file)}/rank`,
    { params: rest },
  );
  return r.data;
}
