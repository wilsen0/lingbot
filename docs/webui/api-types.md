# Frontend API types — staying in sync with the backend

The frontend (Vue/TS in `packages/webui/frontend`) talks to the backend
(FastAPI in `packages/webui/src/linling_webui`) over HTTP. We catch
schema drift at three layers:

1. **OpenAPI snapshot** (`src/api/openapi.snapshot.json`) — the
   committed source of truth. Generated once from a running backend
   and refreshed when the backend's schema legitimately changes.
2. **TS types** (`src/api/openapi.types.ts`) — generated from the
   snapshot by `openapi-typescript`. Imported by the typed client
   (`src/api/typed.ts`); never edited by hand.
3. **Drift tests** — both backend pytest (`test_openapi_snapshot.py`)
   and a frontend `pnpm api:check` script verify the live backend
   matches the snapshot. Either fail loudly when they diverge.

## Day-to-day

You don't need to think about any of this when you're not changing
HTTP shapes.

When you do change a backend route or response model:

```sh
# 1. Make the backend change.
# 2. Run the bot locally so it serves the new schema:
uv run linling run bot/bot.yaml --webui

# 3. Refresh the snapshot + regenerate the TS types:
pnpm --filter @linling/webui-frontend api:update

# 4. The change shows up in `openapi.snapshot.json` and
#    `openapi.types.ts`. Commit both.
# 5. CI re-runs both layers of drift checks.
```

## How the checks fail

* `pytest packages/webui/tests/test_openapi_snapshot.py` runs the live
  FastAPI app in-process, builds its OpenAPI doc, and diffs the
  shape-relevant fields against the committed snapshot. Failing
  message tells you what diverged + how to fix.
* `pnpm --filter @linling/webui-frontend api:check` does the same
  comparison from the JS side, hitting an actually-running backend
  (`API_URL` env var, defaults to `127.0.0.1:8787`).
* `pnpm --filter @linling/webui-frontend typecheck` (i.e.
  `vue-tsc --noEmit`) catches API call sites that haven't been
  migrated to the new shape yet.

## The typed client

`src/api/typed.ts` exposes `apiGet` / `apiPost` / `apiPut` /
`apiPatch` / `apiDel`. Path strings, params, and body / response types
are all checked at compile time against the OpenAPI types. Example:

```ts
import { apiGet, apiPost, type BotStatus, type HotReloadResponse } from "@/api/typed";

const bots: BotStatus[] = await apiGet("/api/bots");
const out: HotReloadResponse = await apiPost("/api/bots/{bot_id}/hot-reload", {
  path: { bot_id: "linling" },
});
```

The legacy modules under `src/api/` still use plain `apiClient` —
migrate them on a per-page basis as you touch them. The drift tests
keep working either way.

## Ignored differences

The check intentionally ignores cosmetic OpenAPI fields:

* `title`, `description`, `examples` — pure documentation
* Any path or component change that only affects those fields

Concrete drifts that **will** trigger the check:

* Adding / removing / renaming an endpoint
* Adding / removing / renaming a schema component
* Adding / removing a parameter
* Changing a parameter's `required` flag or `in` location
* Changing a request / response body schema (incl. types, refs,
  enum values, `additionalProperties`)
