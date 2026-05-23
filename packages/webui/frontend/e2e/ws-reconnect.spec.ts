import { test, expect } from "@playwright/test";

/** WUI-C10 · 观测 · 因缘流切走后再回，WS 自愈且标识回到「红线已牵」 */
test("观测 · 因缘流 WS 自愈", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("掌门").fill("e2e");
  await page.getByLabel("口诀").fill("Op3n-4u!");
  await page.getByRole("button", { name: "结缘" }).click();

  await page.getByRole("button", { name: "菜单" }).click();
  await page.getByRole("link", { name: /观/ }).click();
  await expect(page.getByText("红线已牵")).toBeVisible({ timeout: 10_000 });

  // 回首页再进 — 触发 composable 重建。
  await page.goto("/");
  await page.getByRole("button", { name: "菜单" }).click();
  await page.getByRole("link", { name: /观/ }).click();
  await expect(page.getByText("红线已牵")).toBeVisible({ timeout: 10_000 });
});
