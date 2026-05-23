import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { test, expect } from "@playwright/test";

const AXE_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "../node_modules/axe-core/axe.min.js"),
  "utf-8",
);

async function login(page) {
  await page.goto("/login");
  await page.getByLabel("掌门").fill("e2e");
  await page.getByLabel("口诀").fill("Op3n-4u!");
  await page.getByRole("button", { name: "结缘" }).click();
  await expect(page).toHaveURL(/\/$/);
}

async function scan(page, label: string) {
  await page.evaluate(AXE_SOURCE);
  const result = await page.evaluate(async () => {
    // @ts-expect-error axe injected at runtime
    return await window.axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa"] },
      resultTypes: ["violations"],
    });
  });
  const serious = result.violations.filter(
    (v: { impact?: string }) => v.impact === "serious" || v.impact === "critical",
  );
  if (serious.length) {
    console.log(`[${label}] axe serious/critical:`, JSON.stringify(serious, null, 2));
  }
  return serious;
}

test("axe · 缘起 (login) 无 serious 违规", async ({ page }) => {
  await page.goto("/login");
  const violations = await scan(page, "login");
  expect(violations).toEqual([]);
});

test("axe · 对话 无 serious 违规", async ({ page }) => {
  await login(page);
  const violations = await scan(page, "chat");
  expect(violations).toEqual([]);
});

test("axe · 观测 无 serious 违规", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "菜单" }).click();
  await page.getByRole("link", { name: /观/ }).click();
  const violations = await scan(page, "observatory");
  expect(violations).toEqual([]);
});
