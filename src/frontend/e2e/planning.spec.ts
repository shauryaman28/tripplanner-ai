/**
 * E2E smoke test — the check to run before every change.
 *
 * Roadmap acceptance (Phase 17): type "Plan a 5-day trip to Goa in December for
 * 2 people, budget ₹50000" → wait for SSE planning_complete → itinerary cards render.
 *
 * Also covers register + login, trip creation, the live progress panel, the
 * map (Phase 18), reloading a planned trip, a refinement turn, the PDF
 * download (Phase 19), and a budget conflict whose options survive a reload.
 *
 * Playwright starts its own stack (playwright.config.ts): the stub backend
 * (tests/e2e/stub_backend.py — real app, DB, Redis and graph; external APIs
 * faked) and a frontend pointed at it, on ports of their own. No API keys needed.
 */

import { readFile } from "node:fs/promises";

import { expect, test, type Page } from "@playwright/test";

const EMAIL = `e2e-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;

// The later tests sign in with the account the first one registers.
test.describe.configure({ mode: "serial" });

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

async function createTrip(page: Page, trip: { destination: string; budget: string; interests: string }) {
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill(trip.destination);
  await page.getByLabel("Start date").fill(`${YEAR}-12-10`);
  await page.getByLabel("End date").fill(`${YEAR}-12-12`); // 3 days, 2 nights
  await page.getByLabel(/budget/i).fill(trip.budget);
  await page.getByLabel("Travellers", { exact: true }).fill("2");
  await page.getByLabel(/interests/i).fill(trip.interests);
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  // Redis pub/sub has no replay: only send once the live stream is subscribed.
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();
}

test("register, create trip, plan, see itinerary cards and map, refine", async ({ page }) => {
  // ── 1. Register (auto-login) ─────────────────────────────────────────────
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  // ── 2. Create a trip ─────────────────────────────────────────────────────
  await createTrip(page, { destination: "Goa", budget: "50000", interests: "beach, food" });

  // ── 3. Plan from the chat, with the SSE stream live ──────────────────────
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("Plan a 5-day trip to Goa in December for 2 people, budget ₹50000");
  await chat.press("Enter");

  // ── 4. planning_complete arrives over SSE → itinerary cards render ───────
  const itinerary = page.getByLabel("Your itinerary");
  const progress = page.getByLabel("Planning progress");
  const dayOne = itinerary.getByRole("article").getByText("Day 1", { exact: true }); // the day card, not the map legend
  await expect(dayOne).toBeVisible({ timeout: 90_000 });
  await expect(page.getByLabel("Trip cost").getByText("Estimated total")).toBeVisible();
  await expect(page.getByLabel("Trip cost").getByText(/under budget/)).toBeVisible();
  await expect(progress.getByText(/planning complete/i)).toBeVisible();
  await expect(progress.getByText("Found 1 flight", { exact: true })).toBeVisible();
  await expect(progress.getByText("Found 3 attractions")).toBeVisible();
  await expect(page.getByText(/your itinerary is ready/i)).toBeVisible();
  await expect(chat).toBeEnabled();

  // each stop says what it is; a spare slot is not padded with "free time"
  const cards = itinerary.getByRole("article");
  await expect(cards.first()).toContainText("Fort Aguada");
  await expect(cards.first()).toContainText("Top attraction");
  await expect(cards.first()).toContainText("Heritage site");
  await expect(cards.nth(1)).toContainText("Basilica of Bom Jesus");
  await expect(cards.nth(1)).not.toContainText("Free time");
  // every day of the trip has a card; one with nothing booked says so once
  await expect(cards).toHaveCount(3);
  await expect(cards.nth(2).getByText("Free time")).toHaveCount(1);

  // ── 4b. Map (Phase 18): pins per day, a route, popups, hotel + airport ────
  const map = page.getByLabel("Trip map");
  await map.scrollIntoViewIfNeeded();
  await expect(map.locator(".leaflet-container")).toBeVisible();
  await expect(map.locator('[data-pin="day-1"]')).toHaveCount(2);
  await expect(map.locator('[data-pin="day-2"]')).toHaveCount(1);
  await expect(map.locator('[data-pin="hotel"]')).toHaveCount(1);
  await expect(map.locator('[data-pin="airport"]')).toHaveCount(2);
  // different days, different colours
  const colour = (pin: string) =>
    map.locator(`[data-pin="${pin}"]`).first().evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(await colour("day-1")).not.toBe(await colour("day-2"));
  // day 1 has two stops → one route line; day 2 has one stop → none
  await expect(map.locator("path.route-day-1")).toHaveCount(1);
  await expect(map.locator("path.route-day-2")).toHaveCount(0);
  // pin click → detail popup
  // (by name: while one popup opens the previous one is still fading out)
  const popupFor = (place: string) => map.locator(".leaflet-popup-content").filter({ hasText: place });
  await map.getByTitle(/Day 1 · Morning: Fort Aguada/).click();
  const popup = popupFor("Fort Aguada");
  await expect(popup).toContainText("Day 1 · Morning");
  await expect(popup).toContainText("History");
  await expect(popup).toContainText("Rating: Top attraction (3 of 3)");
  await expect(popup).toContainText("Cost estimate");
  // the legend names every day, and isolates one on request
  const legend = map.getByLabel("Map legend");
  await expect(legend).toContainText("Hotel");
  await legend.getByRole("button", { name: "Day 2" }).click();
  await expect(legend.getByRole("button", { name: "Day 2" })).toHaveAttribute("aria-pressed", "true");
  await legend.getByRole("button", { name: "All days" }).click();
  // a day card's own "Map" button opens that stop's popup
  await itinerary.getByRole("button", { name: "Show Baga Beach on the map" }).click();
  await expect(popupFor("Baga Beach")).toContainText("Day 1 · Afternoon");
  await expect(map.locator(".leaflet-popup-content")).toHaveCount(1); // and the first one has closed
  await expect(map).toBeInViewport();

  // ── 5. A planned trip survives a reload ──────────────────────────────────
  await page.reload();
  await expect(dayOne).toBeVisible();
  await expect(page.getByRole("heading", { name: "Goa", exact: true })).toBeVisible();

  // ── 6. Refinement turn ───────────────────────────────────────────────────
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();
  const downloadPdf = itinerary.getByRole("button", { name: "Download PDF" });
  await expect(downloadPdf).toBeEnabled();
  // (the request is held back a moment, so "while the change is running" lasts long enough to look at)
  await page.route(
    "**/api/trips/*/refine",
    async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 600));
      await route.continue();
    },
    { times: 1 },
  );
  await chat.fill("Change hotels to something closer to the beach");
  await chat.press("Enter");
  await expect(downloadPdf).toBeDisabled(); // the plan on screen is about to be out of date
  await expect(page.getByText(/looking for a different place to stay/i)).toBeVisible();
  // the assistant says what changed — worked out by comparing the two plans
  const changes = page.getByRole("list", { name: "What changed" });
  await expect(changes).toBeVisible({ timeout: 90_000 });
  await expect(changes.getByRole("listitem")).toHaveText(["Stay: Goa Grand → Baga Beach House (same price, ₹4,500 a night)"]);
  await expect(page.getByText("Done. The total stays at ₹17,200.")).toBeVisible();
  await expect(itinerary.getByRole("article").first()).toContainText("Baga Beach House"); // the other hotel
  // only the hotel search ran again; the other two are carried forward as done (not "not started")
  await expect(progress.getByText("Found 2 hotels")).toBeVisible();
  await expect(progress.getByText("Done", { exact: true })).toHaveCount(2);
  await expect(downloadPdf).toBeEnabled(); // and the new plan can be downloaded

  // ── 7. …and a reloaded page still shows every search as done ─────────────
  await page.reload();
  await expect(itinerary.getByRole("article").first()).toContainText("Baga Beach House");
});

test("the itinerary downloads as a PDF: loading state, the file, and a toast when it fails", async ({ page }) => {
  // Roadmap acceptance (Phase 19): the button triggers a real PDF download, the loading state is
  // visible, and a non-200 answer shows the toast "PDF generation failed — try again".
  const exportRoute = "**/api/trips/*/export/pdf";
  await signIn(page);
  await page.getByRole("link", { name: /Goa/ }).click(); // the trip the first test planned
  const itinerary = page.getByLabel("Your itinerary");
  const button = itinerary.getByRole("button", { name: "Download PDF" });
  await expect(button).toBeEnabled();

  // ── 1. A real download. The answer is held back a moment so the loading state can be seen. ──
  await page.route(
    exportRoute,
    async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700));
      await route.continue();
    },
    { times: 1 },
  );
  const downloading = page.waitForEvent("download");
  const answered = page.waitForResponse((response) => response.url().endsWith("/export/pdf"));
  await button.click();
  const preparing = itinerary.getByRole("button", { name: "Preparing PDF…" });
  await expect(preparing).toBeVisible();
  await expect(preparing).toBeDisabled();

  const response = await answered;
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toBe("application/pdf");
  expect(response.headers()["x-itinerary-map"]).toBe("included"); // the stub backend serves the map's tiles

  const download = await downloading;
  expect(download.suggestedFilename()).toBe(`trip-goa-${YEAR}-12-10.pdf`);
  const file = await readFile(await download.path());
  expect(file.subarray(0, 5).toString()).toBe("%PDF-");
  expect(file.length).toBeGreaterThan(30_000); // four pages, the embedded fonts and the map picture
  await expect(button).toBeEnabled(); // "Download PDF" again
  await expect(page.locator("[data-toast]")).toHaveCount(0); // nothing to report

  // ── 2. The map could not be drawn: the PDF still comes, and the page says what is missing. ──
  await page.route(
    exportRoute,
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/pdf",
        headers: { "Content-Disposition": 'attachment; filename="trip-goa.pdf"', "X-Itinerary-Map": "unavailable" },
        body: "%PDF-1.4\n%%EOF\n",
      }),
    { times: 1 },
  );
  const withoutMap = page.waitForEvent("download");
  await button.click();
  expect((await withoutMap).suggestedFilename()).toBe("trip-goa.pdf");
  const notice = page.getByRole("status").filter({ hasText: "PDF downloaded without the map" });
  await expect(notice).toBeVisible();
  await page.getByRole("button", { name: "Dismiss" }).click();
  await expect(notice).toHaveCount(0);

  // ── 3. The export fails: a toast says so, nothing is downloaded, and the button works again. ──
  await page.route(
    exportRoute,
    (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "The PDF could not be generated.",
          error: { code: "INTERNAL_SERVER_ERROR", message: "The PDF could not be generated." },
        }),
      }),
    { times: 1 },
  );
  let downloads = 0;
  page.on("download", () => downloads++);
  await button.click();
  // (by text: Next.js keeps a route announcer with the same role on every page)
  await expect(page.getByRole("alert").filter({ hasText: "PDF generation failed — try again" })).toBeVisible();
  await expect(button).toBeEnabled();
  expect(downloads).toBe(0);

  // ...and the next try goes through
  const retried = page.waitForEvent("download");
  await button.click();
  expect((await retried).suggestedFilename()).toBe(`trip-goa-${YEAR}-12-10.pdf`);
  await expect(page.getByRole("alert").filter({ hasText: "PDF generation failed" })).toHaveCount(0);

  // ── 4. The session has ended: the sign-in page, not a toast that says "try again". ──
  await page.route(
    exportRoute,
    (route) =>
      route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Could not validate credentials" }),
      }),
    { times: 1 },
  );
  await button.click();
  await expect(page).toHaveURL(/\/login$/);
});

test("a budget conflict keeps its options across a reload, and re-plans", async ({ page }) => {
  await signIn(page);
  // ₹8,200 flights out of ₹12,000 leave too little for the rest → the run stops and offers ways out
  await createTrip(page, { destination: "Udaipur", budget: "12000", interests: "history" });

  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("A long weekend of palaces and lakes");
  await chat.press("Enter");

  const options = page.getByLabel("Budget options");
  await expect(options.getByText("Over budget")).toBeVisible({ timeout: 90_000 });
  await expect(options.getByRole("button")).toHaveCount(3);

  // the SSE event that carried the options is gone after a reload — GET /status brings them back
  await page.reload();
  await expect(options.getByRole("button")).toHaveCount(3);
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();

  await options.getByRole("button", { name: /cheaper connecting flights/i }).click();
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });
  // ₹2,500 flights + 2 nights at ₹4,500 = ₹11,500 of ₹12,000: inside the budget, only just
  await expect(page.getByLabel("Trip cost").getByText("Only ₹500 of the budget left")).toBeVisible();
  await expect(page.getByLabel("Planning progress").getByText("Found 1 flight (re-plan attempt 1)")).toBeVisible();
  await expect(options).toHaveCount(0);
});

test("login with existing credentials", async ({ page }) => {
  await signIn(page);
  // the trips planned above are listed
  await expect(page.getByRole("link", { name: /Goa/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /Udaipur/ })).toBeVisible();
});

test("unauthenticated user is redirected to login", async ({ page }) => {
  await page.goto("/login");
  await page.evaluate(() => localStorage.clear());
  await page.goto("/trips");
  await expect(page).toHaveURL(/\/login/);
});
