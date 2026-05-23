import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Static check: every `<button>` and `<router-link>` in Vue SFCs under
 * src/components and src/pages either has `.tap`, `min-h-[44px]`, or is
 * inside a chip/tab where the CSS covers it.
 *
 * This is a cheap static analog of WUI-C11 while Playwright is pending.
 */

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else if (/\.vue$/.test(name)) out.push(full);
  }
  return out;
}

const root = resolve(__dirname, "../src");

function hasTappableClass(line: string): boolean {
  return (
    /\btap\b/.test(line) ||
    /min-h-\[/.test(line) ||
    /ui-chip/.test(line) ||
    /ui-button/.test(line) ||
    /ui-input__field/.test(line) ||
    /class="[^"]*\bp-\d/.test(line) ||
    /\bh-14\b|\bh-12\b|\bh-10\b/.test(line)
  );
}

describe("tap targets (static)", () => {
  it("interactive elements in pages/components appear tap-friendly", () => {
    const violations: string[] = [];
    for (const file of walk(join(root, "pages")).concat(walk(join(root, "components")))) {
      const src = readFileSync(file, "utf-8");
      const lines = src.split("\n");
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (!/<(button|router-link|a\s)/.test(line)) continue;
        // Look at this line and the next five for classes (multi-line attrs).
        const window = lines.slice(i, i + 6).join(" ");
        if (hasTappableClass(window)) continue;
        // Allow invisible buttons (icon-only) inside decor/sheet handles
        if (/aria-hidden|ui-sheet-handle/.test(window)) continue;
        violations.push(`${file}:${i + 1} :: ${line.trim()}`);
      }
    }
    expect(violations, violations.join("\n")).toEqual([]);
  });
});
