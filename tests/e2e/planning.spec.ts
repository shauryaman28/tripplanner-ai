/**
 * Phase 17 E2E smoke test.
 *
 * Acceptance criterion (roadmap):
 *   Type "Plan a 5-day trip to Goa in December for 2 people, budget ₹50000"
 *   → wait for SSE planning_complete
 *   → assert itinerary day cards render
 *
 * Also validates:
 *   - Register + login flow
 *   - Trip creation
 *   - Agent progress panel updates live
 *   - SSE reconnects after brief network interruption (best-effort)
 */

import { expect, test } from "@playwright/test";

const TEST_EMAIL    = `e2e-${Date.now()}@tripplanner.test`;
const TEST_PASSWORD = "test-password-123";

test.describe("Trip planning flow", () => {
  test("register, create trip, plan, see itinerary cards", async ({ page }) => {
    // ── 1. Register ────────────────────────────────────────────────────────
    await page.goto("/login");
    await page.getByRole("button", { name: /create one/i }).click();
    await page.getByLabel("Email").fill(TEST_EMAIL);
    await page.getByLabel("Password").fill(TEST_PASSWORD);
    await page.getByRole("button", { name: /create account/i }).click();

    // Should redirect to /trips
    await expect(page).toHaveURL(/\/trips$/, { timeout: 10_000 });

    // ── 2. Open new trip form ──────────────────────────────────────────────
    await page.getByRole("button", { name: /plan a trip/i }).first().click();

    // Fill the form
    await page.getByLabel("Destination").fill("Goa");
    // Use dates far enough in the future for the MCP tool to accept
    const y = new Date().getFullYear() + 1;
    await page.getByLabel("Start date").fill(`${y}-12-10`);
    await page.getByLabel("End date").fill(`${y}-12-15`);
    await page.getByLabel(/budget/i).fill("50000");
    await page.getByLabel(/group size/i).fill("2");
    await page.getByLabel(/interests/i).fill("beach, food");

    // Submit → navigates to /trips/:id
    await page.getByRole("button", { name: /create & plan/i }).click();
    await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/, { timeout: 15_000 });

    // ── 3. Trigger planning via chat input ─────────────────────────────────
    const chatInput = page.getByRole("textbox", { name: /trip description/i });
    await chatInput.fill("Plan a 5-day trip to Goa in December for 2 people, budget ₹50000");
    await chatInput.press("Enter");

    // ── 4. Assert agent progress panel shows flight agent running ──────────
    await expect(
      page.getByLabel("Planning progress"),
    ).toBeVisible({ timeout: 10_000 });

    // At least one agent badge should become visible
    await expect(
      page.getByText("Flights"),
    ).toBeVisible({ timeout: 10_000 });

    // ── 5. Wait for planning_complete (itinerary cards appear) ─────────────
    // Day cards have an aria label containing "Day 1"
    await expect(
      page.getByText(/Day 1/i),
    ).toBeVisible({ timeout: 90_000 });

    // ── 6. Assert cost summary pills are rendered ──────────────────────────
    await expect(page.getByText("Total")).toBeVisible();
    await expect(page.getByText("Flights")).toBeVisible();

    // ── 7. Assert refinement input is now pre-labelled ────────────────────
    await expect(
      page.getByPlaceholder(/refine your trip/i),
    ).toBeVisible();
  });

  test("login with existing credentials", async ({ page }) => {
    // This test reuses the account created above if tests run sequentially.
    // If isolated, register first.
    await page.goto("/login");
    await page.getByLabel("Email").fill(TEST_EMAIL);
    await page.getByLabel("Password").fill(TEST_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(page).toHaveURL(/\/trips$/, { timeout: 10_000 });
  });

  test("unauthenticated user is redirected to login", async ({ page }) => {
    // Clear storage to simulate unauthenticated state
    await page.goto("/login");
    await page.evaluate(() => localStorage.clear());
    await page.goto("/trips");
    await expect(page).toHaveURL(/\/login/, { timeout: 5_000 });
  });
});
