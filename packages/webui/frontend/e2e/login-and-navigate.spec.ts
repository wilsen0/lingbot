import { test, expect } from "@playwright/test";

async function login(page) {
  await page.goto("/login");
  await page.getByLabel("账号").fill("e2e");
  await page.getByLabel("密码").fill("Op3n-4u!");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/$/);
}

test.describe("登录 → 对话 → 菜单导航", () => {
  test("登录后落在对话页", async ({ page }) => {
    await login(page);
    await expect(page.getByPlaceholder(/输入消息|尚未接入助手/)).toBeVisible();
  });

  test("菜单三项都可达", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "菜单", exact: true }).click();
    await page.getByRole("link", { name: /观测/ }).click();
    await expect(page).toHaveURL(/观测|%E8%A7%82%E6%B5%8B/);
    await expect(page.getByRole("tab", { name: /我的/ })).toBeVisible();

    await page.getByRole("link", { name: "回" }).click();
    await expect(page).toHaveURL(/\/$/);

    await page.getByRole("button", { name: "菜单", exact: true }).click();
    await page.getByRole("link", { name: /设/ }).click();
    await expect(page).toHaveURL(/设置|%E8%AE%BE%E7%BD%AE/);
    await expect(page.getByRole("heading", { name: "已接入" })).toBeVisible();
  });

  test("观测页能打开资产物品", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "菜单" }).click();
    await page.getByRole("link", { name: /观测/ }).click();
    await page.getByRole("tab", { name: /资产/ }).click();
    await expect(page.locator(".asset-panel--mine").getByText("灵玉余额").first()).toBeVisible({
      timeout: 8_000,
    });
  });
});
