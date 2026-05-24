import { test, expect } from "@playwright/test";

test("对话页 · 触达目标 ≥ 44×44", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账号").fill("e2e");
  await page.getByLabel("密码").fill("Op3n-4u!");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/$/);

  const offenders = await page.$$eval("button:visible, a:visible", (els) =>
    els
      .filter((el) => !el.closest("[aria-hidden='true']"))
      .map((el) => {
        const r = el.getBoundingClientRect();
        return { text: (el.textContent || "").trim().slice(0, 40), w: r.width, h: r.height };
      })
      .filter((e) => e.w < 44 || e.h < 44),
  );
  expect(offenders, JSON.stringify(offenders, null, 2)).toEqual([]);
});
