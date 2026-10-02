import { existsSync } from "node:fs";
import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

/**
 * Browser E2E tests. Run from src/frontend:  npx playwright test
 *
 * Needs Docker Postgres + Redis (docker compose up postgres redis -d). The stub
 * backend runs on the repo's own virtualenv (.venv) when there is one, so the
 * venv does not have to be active; PYTHON=/path/to/python picks another.
 *
 * The tests run on their own stack — a stub backend and a second Next.js dev
 * server, on their own ports, database and build directory — so they are
 * deterministic, need no API keys, and can run while the dev servers are up.
 */
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3100);
const API_PORT = Number(process.env.E2E_API_PORT ?? 8100);
const WEB = `http://localhost:${WEB_PORT}`;
const API = `http://localhost:${API_PORT}`;

// The backend is started from the repo root, so a PYTHON given as a path is pinned to this
// directory first — "../../.venv/bin/python" used to be looked up two levels above the repo.
const VENV_PYTHON = path.resolve(__dirname, "../../.venv/bin/python");
const PYTHON = process.env.PYTHON
  ? process.env.PYTHON.includes("/") ? path.resolve(process.env.PYTHON) : process.env.PYTHON
  : existsSync(VENV_PYTHON) ? VENV_PYTHON : "python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false, // the tests share one account; run sequentially
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: WEB,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      // The real app with external APIs stubbed — see tests/e2e/stub_backend.py
      command: `"${PYTHON}" -m tests.e2e.stub_backend`,
      cwd: "../..",
      url: `${API}/ping`,
      env: { PORT: String(API_PORT), CORS_ORIGINS: WEB },
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: `npx next dev --port ${WEB_PORT}`,
      url: `${WEB}/login`,
      // its own build directory, so it never fights a running `npm run dev` over .next
      env: { BACKEND_URL: API, NEXT_PUBLIC_API_URL: API, NEXT_DIST_DIR: ".next-e2e" },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
