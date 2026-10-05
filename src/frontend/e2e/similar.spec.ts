/**
 * Phase 23 — similar trips and search, in the browser.
 *
 * Roadmap acceptance:
 *   - "Similar Trips" renders with the right data: destination, dates, total cost, one highlight;
 *     clicking a card opens that trip
 *   - a beach trip is like another beach trip, not like a trek
 *   - the search input on the trips list works end to end
 *
 * Runs on the Playwright stack's stub backend (tests/e2e/stub_backend.py). Its stand-in embedder
 * makes two texts as close as the words they share (tests/fakes.py), and its attractions search
 * finds beaches and forts everywhere but Leh, where it finds a lake, a monastery and a pass — so
 * the Goa and Varkala trips are alike, and the Leh trip is like neither.
 */

import { expect, test, type Page } from "@playwright/test";

const EMAIL = `similar-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;

test.describe.configure({ mode: "serial" }); // one account, three trips planned by the first test

async function signIn(page: Page, register = false) {
  await page.goto("/login");
  if (register) await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: register ? /create account/i : "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

/** Create a three-day trip starting on `day` August and plan it. Resolves when the day cards are on the page. */
async function planTrip(page: Page, destination: string, day: number, interests: string) {
  const date = (offset: number) => `${YEAR}-08-${String(day + offset).padStart(2, "0")}`;
  await page.goto("/trips");
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill(destination);
  await page.getByLabel("Start date").fill(date(0));
  await page.getByLabel("End date").fill(date(2));
  await page.getByLabel(/budget/i).fill("50000");
  await page.getByLabel("Travellers", { exact: true }).fill("2");
  await page.getByLabel(/interests/i).fill(interests);
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.locator('[data-stream="connected"]')).toBeAttached(); // pub/sub has no replay
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("Plan it");
  await chat.press("Enter");
  await expect(page.getByLabel("Your itinerary").getByRole("article")).toHaveCount(3, { timeout: 90_000 });
}

const similarTrips = (page: Page) => page.getByRole("region", { name: "Similar trips" });
const searchBox = (page: Page) => page.getByRole("searchbox", { name: "Search your trips" });
const searchResults = (page: Page) => page.getByRole("region", { name: "Search results" });

async function search(page: Page, words: string) {
  await searchBox(page).fill(words);
  await searchBox(page).press("Enter");
  await expect(searchResults(page).getByRole("status")).toContainText(`“${words}”`);
}

test("a beach trip's similar trips are the other beach trip — a card that opens it — and not the trek", async ({ page }) => {
  await signIn(page, true);
  await planTrip(page, "Varkala", 3, "beach, food");
  await planTrip(page, "Leh", 7, "trekking, monasteries");
  await planTrip(page, "Goa", 11, "beach, food");

  // The section arrives by itself: a plan's embedding is made just after it is saved, and the page asks again.
  const similar = similarTrips(page);
  await expect(similar).toBeVisible({ timeout: 20_000 });
  await expect(similar).toContainText("Other trips of yours most like this one.");

  // ── one card: the other beach trip, with what the roadmap asks of a thumbnail ──
  const cards = similar.getByRole("link");
  await expect(cards).toHaveCount(1);
  const card = cards.first();
  await expect(card.getByRole("heading", { level: 3 })).toHaveText("Varkala"); // destination
  await expect(card).toContainText(/3 – 5 Aug \d{4}/); // dates
  await expect(card).toContainText("₹17,200 in all"); // total cost
  await expect(card).toContainText("Fort Aguada"); // one highlight: its most popular stop
  await expect(similar).not.toContainText("Leh");

  // it is the last thing in the plan: under the map
  const top = (locator: ReturnType<Page["locator"]>) => locator.evaluate((element) => element.getBoundingClientRect().top);
  expect(await top(similar)).toBeGreaterThan(await top(page.locator("#trip-map")));

  // ── clicking it opens that trip — its own plan, and its own similar trips ──
  const goaUrl = page.url();
  await card.click();
  await expect(page).not.toHaveURL(goaUrl);
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Varkala");
  await expect(page.getByLabel("Your itinerary").getByRole("article")).toHaveCount(3);
  await expect(similarTrips(page).getByRole("heading", { level: 3 })).toHaveText(["Goa"]);

  // ── still there after a reload: it is asked of the backend, not remembered by the page ──
  await page.reload();
  await expect(similarTrips(page).getByRole("heading", { level: 3 })).toHaveText(["Goa"]);
});

test("a trip like no other has no Similar trips section, and no error", async ({ page }) => {
  await signIn(page);
  const answered = page.waitForResponse((response) => response.url().endsWith("/similar"));
  await page.getByRole("heading", { name: "Leh", exact: true }).click();
  expect(await (await answered).json()).toEqual({ status: "ready", results: [] }); // asked and answered: nothing is alike
  await expect(page.getByLabel("Your itinerary").getByRole("article")).toHaveCount(3);
  await expect(page.locator("#trip-map")).toBeVisible();
  await expect(similarTrips(page)).toHaveCount(0);
  await expect(page.getByText("Similar trips")).toHaveCount(0);
  await expect(page.locator("[data-toast]")).toHaveCount(0);
});

test("while a plan's embedding is still being made, the page asks again until it is there", async ({ page }) => {
  // On the real stack the embedding is written a second or so after the plan (the stub is instant):
  // the backend answers "pending" until then. Here the first two answers are made to say so.
  let asked = 0;
  await page.route("**/api/trips/*/similar", async (route) => {
    asked += 1;
    if (asked <= 2) return route.fulfill({ status: 200, contentType: "application/json", body: '{"status":"pending","results":[]}' });
    return route.continue();
  });
  await signIn(page);
  await page.getByRole("heading", { name: "Goa", exact: true }).click();
  await expect(page.getByLabel("Your itinerary").getByRole("article")).toHaveCount(3);
  await expect(similarTrips(page).getByRole("heading", { level: 3 })).toHaveText(["Varkala"], { timeout: 20_000 });
  expect(asked).toBeGreaterThanOrEqual(3);
});

test("three similar trips are shown, the most alike first, however many there are", async ({ page }) => {
  // Roadmap: three thumbnails. The backend returns up to five; this account has one — so five are made up here.
  const card = (n: number) => ({
    trip: { id: `00000000-0000-4000-8000-00000000000${n}`, user_id: "u", destination: `Beach ${n}`, start_date: `${YEAR}-01-0${n}`, end_date: `${YEAR}-01-0${n + 2}`, budget: 50000, group_size: 2, interests: ["beach"], status: "completed", created_at: "2026-01-01T00:00:00Z" },
    itinerary_id: `10000000-0000-4000-8000-00000000000${n}`,
    total_cost: n === 2 ? null : 10_000 * n,
    highlight: n === 3 ? null : `Shore ${n}`,
    similarity: 1 - n / 100,
  });
  await page.route("**/api/trips/*/similar", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ready", results: [1, 2, 3, 4, 5].map(card) }) }),
  );
  await signIn(page);
  await page.getByRole("heading", { name: "Goa", exact: true }).click();
  const cards = similarTrips(page).getByRole("link");
  await expect(cards).toHaveCount(3);
  await expect(similarTrips(page).getByRole("heading", { level: 3 })).toHaveText(["Beach 1", "Beach 2", "Beach 3"]);
  await expect(cards.nth(0)).toContainText("₹10,000 in all");
  await expect(cards.nth(0)).toContainText("Shore 1");
  await expect(cards.nth(1)).not.toContainText("in all"); // a plan with no total says nothing about it…
  await expect(cards.nth(2)).not.toContainText("Shore"); // …and one with no place to name leaves that out
  await expect(cards.nth(0)).toHaveAttribute("href", "/trips/00000000-0000-4000-8000-000000000001");
});

test("when similar trips cannot be asked for, the section is just not there", async ({ page }) => {
  await page.route("**/api/trips/*/similar", (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"boom"}' }));
  await signIn(page);
  await page.getByRole("heading", { name: "Goa", exact: true }).click();
  await expect(page.getByLabel("Your itinerary").getByRole("article")).toHaveCount(3);
  await expect(page.locator("#trip-map")).toBeVisible();
  await expect(similarTrips(page)).toHaveCount(0);
  await expect(page.locator("[data-toast]")).toHaveCount(0); // an extra: not worth a message
});

test("the trips list is searched by what a trip is about", async ({ page }) => {
  await signIn(page);
  const cards = page.getByRole("main").getByRole("heading", { level: 2 });
  await expect(cards).toHaveText(["Goa", "Leh", "Varkala"]); // newest first
  await expect(searchBox(page)).toHaveAttribute("placeholder", /Search your trips/);

  // ── "beach": the two beach trips, each with what its plan came to and a place in it ──
  await search(page, "beach");
  const results = searchResults(page);
  await expect(results.getByRole("status")).toHaveText("2 trips match “beach”, the closest first.");
  // the two are equally close to one word (the stand-in embedder counts words): either may come first
  await expect(results.getByRole("link")).toHaveCount(2);
  expect((await results.getByRole("heading", { level: 2 }).allTextContents()).sort()).toEqual(["Goa", "Varkala"]);
  await expect(results).not.toContainText("Leh");
  const first = results.getByRole("link").first();
  await expect(first).toContainText("₹17,200 of ₹50,000 budget");
  await expect(first).toContainText("Fort Aguada");
  expect(Number(await first.getAttribute("data-similarity"))).toBeGreaterThan(0.15);

  // ── "monastery": the trek, and only it ──
  await search(page, "monastery");
  await expect(results.getByRole("status")).toHaveText("1 trip matches “monastery”, the closest first.");
  await expect(results.getByRole("heading", { level: 2 })).toHaveText(["Leh"]);
  await expect(results.getByRole("link").first()).toContainText("Pangong Lake");

  // ── nothing like it: said so, with what the search goes by ──
  await search(page, "qwertyuiop");
  await expect(results.getByRole("status")).toHaveText("No trips match “qwertyuiop”.");
  await expect(results.getByRole("link")).toHaveCount(0);
  await expect(results).toContainText("by what they are about");

  // ── back to every trip: the button, or just emptying the box ──
  await results.getByRole("button", { name: "Show all trips" }).click();
  await expect(results).toHaveCount(0);
  await expect(cards).toHaveCount(3);
  await expect(searchBox(page)).toHaveValue("");
  await search(page, "beach");
  await searchBox(page).fill("");
  await expect(searchResults(page)).toHaveCount(0);
  await expect(cards).toHaveCount(3);

  // ── a result is a trip card like any other: it opens the trip ──
  await search(page, "monastery");
  await results.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Leh");
});

test("a search that cannot run says so and leaves the list as it was", async ({ page }) => {
  await signIn(page);
  await page.route("**/api/trips/search*", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Search is not available right now. Try again in a moment." }) }),
  );
  await searchBox(page).fill("beach");
  await searchBox(page).press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "Search is not available right now. Try again in a moment." })).toBeVisible();
  await expect(searchResults(page)).toHaveCount(0);
  await expect(page.getByRole("main").getByRole("heading", { level: 2 })).toHaveCount(3);

  // and when it can again, the message goes
  await page.unroute("**/api/trips/search*");
  await searchBox(page).press("Enter");
  await expect(searchResults(page).getByRole("status")).toContainText("2 trips match");
  await expect(page.getByRole("alert").filter({ hasText: "Search is not available" })).toHaveCount(0);
});

test("search and similar trips fit a phone", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const fits = async (where: string) => {
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `${where}: the page scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(0);
  };
  await signIn(page);
  await search(page, "beach");
  await expect(searchResults(page).getByRole("link")).toHaveCount(2);
  await fits("search results");
  expect((await searchBox(page).boundingBox())!.height).toBeGreaterThanOrEqual(40);

  await searchResults(page).getByRole("heading", { name: "Goa", exact: true }).click();
  await expect(similarTrips(page)).toBeVisible({ timeout: 20_000 });
  await similarTrips(page).scrollIntoViewIfNeeded();
  await fits("similar trips");
});
