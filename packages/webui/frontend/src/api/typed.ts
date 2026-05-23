/**
 * Type-safe API client.
 *
 * Wraps the existing axios `apiClient` (which carries auth + refresh
 * logic) with strict OpenAPI-derived types. New code should call
 * `api.<get|post|put|patch|del>` instead of the legacy
 * `apiClient.get<MyType>("/path")` pattern — the path string and
 * payload shape are checked against `openapi.types.ts` at compile
 * time, so a backend rename / schema change surfaces as a TypeScript
 * error in `pnpm typecheck` instead of a 500 in production.
 *
 * Migration is incremental: every legacy `apiClient.<method>` call
 * can be left as-is and converted later.
 */

import { apiClient } from "./client";
import type { paths, components } from "./openapi.types";

// Re-export schema component types so callers don't need to dig into
// the generated `components["schemas"]["Foo"]` form.
export type Schemas = components["schemas"];

/** Convenient shortcuts for the most common schema types. */
export type BotStatus = Schemas["BotStatus"];
export type LoginResponse = Schemas["TokenResponse"];
export type ProfileResponse = Schemas["ProfileResponse"];
export type EventEnvelope = Schemas["EventEnvelope"];
export type AuditEntry = Schemas["AuditEntry"];
export type RuleSummary = Schemas["RuleSummary"];
export type RuleFile = Schemas["RuleFile"];
export type RuleFileContent = Schemas["RuleFileContent"];
export type RuleLintResult = Schemas["RuleLintResult"];
export type RuleFileSaveRequest = Schemas["RuleFileSaveRequest"];
export type RuleFileSaveResult = Schemas["RuleFileSaveResult"];
export type AgentSummary = Schemas["AgentSummary"];
export type HealthResponse = Schemas["HealthResponse"];
export type HotReloadResponse = Schemas["HotReloadResponse"];
export type SettingsResponse = Schemas["SettingsResponse"];
export type KvNamespace = Schemas["KvNamespace"];
export type KvPage = Schemas["KvPage"];
export type KvRow = Schemas["KvRow"];
export type KvRankResponse = Schemas["KvRankResponse"];

// ---------------------------------------------------------------------------
// Helpers — extract method-specific types from the openapi `paths` map.
// ---------------------------------------------------------------------------

type PathKey = keyof paths;
type Method<P extends PathKey> = keyof paths[P];

/** Body type accepted by ``POST /path`` etc., or ``never`` when none. */
type RequestBody<
  P extends PathKey,
  M extends Method<P>,
> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

/** Path parameters, e.g. ``/api/bots/{bot_id}`` → ``{ bot_id: string }``. */
type PathParams<
  P extends PathKey,
  M extends Method<P>,
> = paths[P][M] extends { parameters: { path: infer Q } }
  ? Q
  : never;

/** Query parameters. */
type QueryParams<
  P extends PathKey,
  M extends Method<P>,
> = paths[P][M] extends { parameters: { query?: infer Q } }
  ? NonNullable<Q>
  : never;

/** Successful (200/201/204) response body, or ``void`` for empty. */
type ResponseBody<
  P extends PathKey,
  M extends Method<P>,
> = paths[P][M] extends {
  responses: { 200: { content: { "application/json": infer R } } };
}
  ? R
  : paths[P][M] extends { responses: { 201: { content: { "application/json": infer R } } } }
    ? R
    : void;

// ---------------------------------------------------------------------------
// Client surface
// ---------------------------------------------------------------------------

/**
 * Substitute path placeholders ``{name}`` with actual values.
 *
 * The OpenAPI schema is the source of truth for placeholder names; we
 * fail loudly if the caller forgot one rather than silently shipping
 * a literal ``{bot_id}`` to the server.
 */
function fillPath(path: string, params?: Record<string, string | number>): string {
  if (!params) return path;
  return path.replace(/\{([^}]+)\}/g, (_, key: string) => {
    const v = params[key];
    if (v === undefined || v === null) {
      throw new Error(`missing path param '${key}' for ${path}`);
    }
    return encodeURIComponent(String(v));
  });
}

interface CallOpts<P extends PathKey, M extends Method<P>> {
  path?: PathParams<P, M> extends never ? never : PathParams<P, M>;
  query?: QueryParams<P, M> extends never ? never : QueryParams<P, M>;
  body?: RequestBody<P, M> extends never ? never : RequestBody<P, M>;
}

/** Typed GET. */
export async function apiGet<P extends PathKey & keyof paths>(
  path: P,
  opts?: paths[P] extends { get: unknown } ? CallOpts<P, "get"> : never,
): Promise<paths[P] extends { get: unknown } ? ResponseBody<P, "get"> : never> {
  const url = fillPath(path as string, opts?.path as Record<string, string | number> | undefined);
  const r = await apiClient.get(url, { params: opts?.query });
  return r.data;
}

/** Typed POST. */
export async function apiPost<P extends PathKey & keyof paths>(
  path: P,
  opts?: paths[P] extends { post: unknown } ? CallOpts<P, "post"> : never,
): Promise<paths[P] extends { post: unknown } ? ResponseBody<P, "post"> : never> {
  const url = fillPath(path as string, opts?.path as Record<string, string | number> | undefined);
  const r = await apiClient.post(url, opts?.body, { params: opts?.query });
  return r.data;
}

/** Typed PUT. */
export async function apiPut<P extends PathKey & keyof paths>(
  path: P,
  opts?: paths[P] extends { put: unknown } ? CallOpts<P, "put"> : never,
): Promise<paths[P] extends { put: unknown } ? ResponseBody<P, "put"> : never> {
  const url = fillPath(path as string, opts?.path as Record<string, string | number> | undefined);
  const r = await apiClient.put(url, opts?.body, { params: opts?.query });
  return r.data;
}

/** Typed PATCH. */
export async function apiPatch<P extends PathKey & keyof paths>(
  path: P,
  opts?: paths[P] extends { patch: unknown } ? CallOpts<P, "patch"> : never,
): Promise<paths[P] extends { patch: unknown } ? ResponseBody<P, "patch"> : never> {
  const url = fillPath(path as string, opts?.path as Record<string, string | number> | undefined);
  const r = await apiClient.patch(url, opts?.body, { params: opts?.query });
  return r.data;
}

/** Typed DELETE. ``del`` because ``delete`` is a reserved word. */
export async function apiDel<P extends PathKey & keyof paths>(
  path: P,
  opts?: paths[P] extends { delete: unknown } ? CallOpts<P, "delete"> : never,
): Promise<paths[P] extends { delete: unknown } ? ResponseBody<P, "delete"> : never> {
  const url = fillPath(path as string, opts?.path as Record<string, string | number> | undefined);
  const r = await apiClient.delete(url, { params: opts?.query });
  return r.data;
}
