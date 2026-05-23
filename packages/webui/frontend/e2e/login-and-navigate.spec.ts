import { test, expect } from "@playwright/test";

async function login(page) {
  await page.goto("/login");
  await page.getByLabel("掌门").fill("e2e");
  await page.getByLabel("口诀").fill("Op3n-4u!");
  await page.getByRole("button", { name: "结缘" }).click();
  await expect(page).toHaveURL(/\/$/);
}

test.describe("登录 → 对话 → 菜单导航", () => {
  test("登录后落在对话页", async ({ page }) => {
    await login(page);
    await expect(page.getByPlaceholder(/言於此|未见红娘/)).toBeVisible();
  });

  test("菜单三项都可达", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "菜单" }).click();
    await page.getByRole("link", { name: /观/ }).click();
    await expect(page).toHaveURL(/观测|%E8%A7%82%E6%B5%8B/);
    await expect(page.getByRole("button").filter({ hasText: /因/ }).first()).toBeVisible();

    await page.getByRole("link", { name: "回" }).click();
    await expect(page).toHaveURL(/\/$/);

    await page.getByRole("button", { name: "菜单" }).click();
    await page.getByRole("link", { name: /司/ }).click();
    await expect(page).toHaveURL(/设置|%E8%AE%BE%E7%BD%AE/);
    await expect(page.getByRole("heading", { name: "在册" })).toBeVisible();
  });

  test("观测页能打开灵玉目录", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "菜单" }).click();
    await page.getByRole("link", { name: /观/ }).click();
    await page.getByRole("button", { name: "灵玉" }).click();
    await expect(page.getByText("榜 / 分数")).toBeVisible({ timeout: 8_000 });
  });
});
