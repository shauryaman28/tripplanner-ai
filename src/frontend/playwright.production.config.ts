import { defineConfig, devices } from "@playwright/test";

/**
 * One check, in a real browser, of a site that is already running (Phase 27):
 *
 *   E2E_PRODUCTION_URL=https://your-app.vercel.app npx playwright test -c playwright.production.config.ts
 *   npx playwright test -c playwright.production.config.ts          # the production-like stack: http://localhost:3200
 *
 * Unlike the everyday suite (playwright.config.ts) it starts nothing and stubs nothing: the site at that
 * address, its real backend, the real providers and models. It makes one account and plans one trip.
 */
export default defineConfig({
  testDir: "./e2e-production",
  timeout: 240_000,
  expect: { timeout: 20_000 },
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_PRODUCTION_URL ?? "http://localhost:3200",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
