import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const tokens = readFileSync(resolve(__dirname, "../src/theme/tokens.css"), "utf-8");

const REQUIRED_VARS = [
  "--color-bg",
  "--color-bg-veil",
  "--color-ink",
  "--color-ink-soft",
  "--color-sorrow",
  "--color-thread",
  "--color-bell",
  "--color-petal",
  "--color-jade",
  "--color-alert",
  "--color-ash",
];

function extractBlock(marker: RegExp): string {
  const start = tokens.search(marker);
  if (start < 0) throw new Error(`marker ${marker} not found in tokens.css`);
  const rest = tokens.slice(start);
  const end = rest.indexOf("}");
  return rest.slice(0, end + 1);
}

describe("theme tokens", () => {
  it("dark (default) mode defines every required color var", () => {
    const block = extractBlock(/:root,\s*:root\[data-theme="auto"\],\s*:root\[data-theme="dark"\]/);
    for (const name of REQUIRED_VARS) {
      expect(block, `missing ${name} in dark`).toContain(name);
    }
  });

  it("light mode defines every required color var", () => {
    const block = extractBlock(/:root\[data-theme="light"\]/);
    for (const name of REQUIRED_VARS) {
      expect(block, `missing ${name} in light`).toContain(name);
    }
  });

  it("defines motion tokens", () => {
    for (const name of [
      "--motion-bell-swing",
      "--motion-petal-fall",
      "--motion-breeze-drift",
      "--motion-thread-draw",
      "--motion-fade-in-up",
      "--motion-sheet-rise",
      "--motion-tap",
    ]) {
      expect(tokens).toContain(name);
    }
  });

  it("honours prefers-reduced-motion by hiding decorative layers", () => {
    expect(tokens).toContain("@media (prefers-reduced-motion: reduce)");
    expect(tokens).toContain(".decor-petal");
    expect(tokens).toContain(".decor-breeze");
  });
});
