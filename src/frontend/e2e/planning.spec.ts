/**
 * Phase 17 E2E smoke test — the check to run before every change.
 *
 * Roadmap acceptance: type "Plan a 5-day trip to Goa in December for 2 people,
 * budget ₹50000" → wait for SSE planning_complete → itinerary cards render.
 *
 * Also covers register + login, trip creation, the live progress panel,
 * reloading a planned trip, and a refinement turn.
 *
 * Runs against whatever is on :8000. By default Playwright starts the stub
 * backend (tests/e2e/stub_backend.py — real app, DB, Redis and graph; external
 * APIs faked), so no API keys are needed.
 */

import { expect, test } from "@playwright/test";

const EMAIL = `e2e-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";

// The second test logs in with the account the first one registers.
test.describe.configure({ mode: "serial" });

test("register, create trip, plan, see itinerary cards, refine", async ({ page }) => {
  // ── 1. Register (auto-login) ─────────────────────────────────────────────
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  // ── 2. Create a trip ─────────────────────────────────────────────────────
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  const year = new Date().getFullYear() + 1;
  await page.getByLabel("Destination").fill("Goa");
  await page.getByLabel("Start date").fill(`${year}-12-10`);
  await page.getByLabel("End date").fill(`${year}-12-15`);
  await page.getByLabel(/budget/i).fill("50000");
  await page.getByLabel(/group size/i).fill("2");
  await page.getByLabel(/interests/i).fill("beach, food");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);

  // ── 3. Plan from the chat, with the SSE stream live ──────────────────────
  const progress = page.getByLabel("Planning progress");
  await expect(progress.getByText("Live")).toBeVisible();

  const chat = page.getByRole("textbox", { name: /trip description/i });
  await chat.fill("Plan a 5-day trip to Goa in December for 2 people, budget ₹50000");
  await chat.press("Enter");

  // ── 4. planning_complete arrives over SSE → itinerary cards render ───────
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByText("Day 1", { exact: true })).toBeVisible({ timeout: 90_000 });
  await expect(itinerary.getByText("Total", { exact: true })).toBeVisible();
  await expect(progress.getByText(/planning complete/i)).toBeVisible();
  await expect(progress.getByText(/found \d+ flights/i)).toBeVisible();
  await expect(page.getByPlaceholder(/refine your trip/i)).toBeVisible();

  // ── 5. A planned trip survives a reload ──────────────────────────────────
  await page.reload();
  await expect(itinerary.getByText("Day 1", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Goa" })).toBeVisible();

  // ── 6. Refinement turn ───────────────────────────────────────────────────
  await expect(progress.getByText("Live")).toBeVisible();
  await chat.fill("Change hotels to something closer to the beach");
  await chat.press("Enter");
  await expect(page.getByText(/refining your trip/i)).toBeVisible();
  await expect(page.getByPlaceholder(/refine your trip/i)).toBeVisible({ timeout: 90_000 });
  await expect(itinerary.getByText("Day 1", { exact: true })).toBeVisible();
});

test("login with existing credentials", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/trips$/);
  await expect(page.getByText("Goa")).toBeVisible(); // the trip planned above is listed
});

test("unauthenticated user is redirected to login", async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => localStorage.clear());
  await page.goto("/trips");
  await expect(page).toHaveURL(/\/login/);
});
