import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config using the system-installed Google Chrome 148.
 * Launches uvicorn as the web server and drives the built SPA at :8788
 * so it doesn't collide with dev server on :5173.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8788",
    trace: "retain-on-failure",
    channel: "chrome",
    viewport: { width: 390, height: 844 }, // iPhone 14 Pro-ish
    hasTouch: true,
    isMobile: true,
  },
  projects: [
    {
      name: "mobile",
      use: { ...devices["Pixel 7"], channel: "chrome" },
    },
  ],
  webServer: {
    command:
      "uv run --no-sync python -m linling_webui.scripts.e2e_harness --port 8788",
    cwd: "../../..",
    url: "http://127.0.0.1:8788/api/health",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
