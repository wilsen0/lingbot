#!/usr/bin/env node
/**
 * Compare the running backend's OpenAPI document against the snapshot
 * checked into ``src/api/openapi.snapshot.json``.
 *
 * Used by ``pnpm run check:api`` so CI can guarantee that the
 * generated TS types haven't drifted from the live API. Exits 0 if
 * the structural shapes are identical (paths set, request/response
 * schemas), 1 otherwise. Cosmetic-only changes (titles, FastAPI
 * auto-generated descriptions) are diffed but don't fail the check.
 *
 * Usage:
 *   node scripts/check-api.mjs                       # default URL
 *   API_URL=http://other:8888/api node scripts/check-api.mjs
 *   node scripts/check-api.mjs --update              # rewrite snapshot
 */

import { readFileSync, writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { argv, env, exit } from "node:process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SNAPSHOT = resolve(__dirname, "..", "src/api/openapi.snapshot.json");
const API_URL = env.API_URL ?? "http://127.0.0.1:8787/api";
const ENDPOINT = `${API_URL.replace(/\/$/, "")}/openapi.json`;

async function fetchLive() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 5_000);
  try {
    const r = await fetch(ENDPOINT, { signal: ctrl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Return only the fields we care about for drift — paths, methods,
 * request/response schemas and parameter names. Stripping
 * ``summary``/``description``/``title`` fields means the check tolerates
 * doc-only edits without forcing a snapshot bump.
 */
function shape(doc) {
  const out = { paths: {}, schemas: {} };
  for (const [path, methods] of Object.entries(doc.paths ?? {})) {
    out.paths[path] = {};
    for (const [m, op] of Object.entries(methods ?? {})) {
      if (m.startsWith("x-") || m === "parameters") continue;
      out.paths[path][m] = {
        params: (op.parameters ?? []).map((p) => `${p.in}:${p.name}:${p.required ?? false}`).sort(),
        body: stripDocs(op.requestBody?.content?.["application/json"]?.schema ?? null),
        responses: Object.fromEntries(
          Object.entries(op.responses ?? {}).map(([code, r]) => [
            code,
            stripDocs(r.content?.["application/json"]?.schema ?? null),
          ]),
        ),
      };
    }
  }
  for (const [name, schema] of Object.entries(doc.components?.schemas ?? {})) {
    out.schemas[name] = stripDocs(schema);
  }
  return out;
}

function stripDocs(node) {
  if (node === null || typeof node !== "object") return node;
  if (Array.isArray(node)) return node.map(stripDocs);
  const out = {};
  for (const [k, v] of Object.entries(node)) {
    if (k === "title" || k === "description" || k === "examples") continue;
    out[k] = stripDocs(v);
  }
  return out;
}

function diff(a, b, path = "") {
  const diffs = [];
  // Primitive (or one side null) → string-compare via JSON.
  const aIsObj = a !== null && typeof a === "object" && !Array.isArray(a);
  const bIsObj = b !== null && typeof b === "object" && !Array.isArray(b);
  if (!aIsObj || !bIsObj) {
    if (JSON.stringify(a) !== JSON.stringify(b)) {
      diffs.push(`${path || "(root)"}: ${JSON.stringify(a)} → ${JSON.stringify(b)}`);
    }
    return diffs;
  }
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) {
    const child = path ? `${path}.${k}` : k;
    if (!(k in a)) {
      diffs.push(`+ ${child}`);
    } else if (!(k in b)) {
      diffs.push(`- ${child}`);
    } else {
      diffs.push(...diff(a[k], b[k], child));
    }
  }
  return diffs;
}

async function main() {
  const update = argv.includes("--update");
  let live;
  try {
    live = await fetchLive();
  } catch (e) {
    console.error(`✗ failed to fetch ${ENDPOINT}: ${e.message}`);
    console.error(
      "  Start the backend (`uv run linling run bot/bot.yaml --webui`) " +
        "or set API_URL.",
    );
    exit(2);
  }

  if (update) {
    writeFileSync(SNAPSHOT, JSON.stringify(live, null, 2) + "\n", "utf-8");
    console.log(`✓ snapshot updated at ${SNAPSHOT}`);
    console.log(
      "  Now regenerate TS types: " +
        "`npx openapi-typescript src/api/openapi.snapshot.json -o src/api/openapi.types.ts`",
    );
    return;
  }

  let snap;
  try {
    snap = JSON.parse(readFileSync(SNAPSHOT, "utf-8"));
  } catch (e) {
    console.error(`✗ snapshot missing or unreadable: ${e.message}`);
    console.error("  Run with --update to seed it.");
    exit(2);
  }

  const a = shape(snap);
  const b = shape(live);
  const diffs = diff(a, b);
  if (diffs.length === 0) {
    console.log("✓ OpenAPI matches snapshot");
    return;
  }
  console.error(`✗ OpenAPI drift detected (${diffs.length} changes):`);
  for (const d of diffs.slice(0, 60)) console.error("  " + d);
  if (diffs.length > 60) console.error(`  … and ${diffs.length - 60} more`);
  console.error("");
  console.error("If the live API is the source of truth, run:");
  console.error("  pnpm run check:api -- --update");
  console.error("  npx openapi-typescript src/api/openapi.snapshot.json -o src/api/openapi.types.ts");
  exit(1);
}

main();
