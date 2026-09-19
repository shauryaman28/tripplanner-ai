import { defineConfig, devices } from "@playwright/test";

/**
 * Phase 17 E2E tests.
 *
 * Requires:
 *   1. FastAPI backend running at http://localhost:8000
 *   2. Next.js frontend running at http://localhost:3000
 *      (npm run dev inside src/frontend/)
 *
 * Run: npx playwright test
 */
export default defineConfig({
  testDir: "../../tests/e2e",
  timeout: 90_000,          // planning can take up to 30s + SSE delivery
  expect: { timeout: 15_000 },
  fullyParallel: false,     // SSE tests share backend state; run sequentially
  retries: 1,
  reporter: [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // Auto-start the Next.js dev server for CI (comment out if already running)
  // webServer: {
  //   command: "npm run dev",
  //   url: "http://localhost:3000",
  //   reuseExistingServer: true,
  //   timeout: 60_000,
  // },
});
