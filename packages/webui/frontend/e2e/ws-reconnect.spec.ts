import { test, expect } from "@playwright/test";

/** WUI-C10 · 观测 · 我的记录切走后再回，WS 自愈且标识回到「只看自己的记录」 */
test("观测 · 我的记录 WS 自愈", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账号").fill("e2e");
  await page.getByLabel("密码").fill("Op3n-4u!");
  await page.getByRole("button", { name: "登录" }).click();

  await page.getByRole("button", { name: "菜单" }).click();
  await page.getByRole("link", { name: /观测/ }).click();
  await expect(page.getByText("只看自己的消息")).toBeVisible({ timeout: 10_000 });

  // 回首页再进 — 触发 composable 重建。
  await page.goto("/");
  await page.getByRole("button", { name: "菜单" }).click();
  await page.getByRole("link", { name: /观测/ }).click();
  await expect(page.getByText("只看自己的消息")).toBeVisible({ timeout: 10_000 });
});
