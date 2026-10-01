import { defineConfig, devices } from "@playwright/test";

/**
 * Phase 17 E2E tests. Run from src/frontend:  npx playwright test
 *
 * Needs Docker Postgres + Redis (docker compose up postgres redis -d) and the
 * Python venv on PATH (or PYTHON=/path/to/python). Both servers below are
 * started automatically unless something is already listening — start the real
 * backend on :8000 first to run the same test against real API keys.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false, // the tests share one account; run sequentially
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      // The real app with external APIs stubbed — see tests/e2e/stub_backend.py
      command: `${process.env.PYTHON ?? "python"} -m tests.e2e.stub_backend`,
      cwd: "../..",
      url: "http://localhost:8000/ping",
      reuseExistingServer: true,
      timeout: 60_000,
    },
    {
      command: "npm run dev",
      url: "http://localhost:3000/login",
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
