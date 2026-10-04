/**
 * Phase 20 — frontend polish, in the browser.
 *
 * Roadmap acceptance:
 *   - itinerary text appears token by token while the builder writes
 *   - a refinement animates only the section it changes, then marks what changed
 *   - error states: which search failed, with a Retry; a budget conflict's options as cards
 *   - every view works at 375px wide, and the planning progress is a bottom sheet there
 *   - a Playwright test for the refinement flow
 *
 * Runs on the Playwright stack's stub backend (tests/e2e/stub_backend.py). What
 * its providers do depends on the destination — Hampi / Badami: the hotel search
 * is down until retried; Shimla / Manali: every search is down until the trip is
 * retried; Kaza: no airport is known for it, so the flight search always fails.
 * Each test plans on dates of its own, so the stub's per-search counters of one
 * test never reach another.
 *
 * Some states last less than a second against the stub (the draft, "Updating…"),
 * so the page is watched with a MutationObserver that records every state it
 * passes through — the assertions do not depend on catching a moment.
 */

import { expect, test, type Page } from "@playwright/test";

const EMAIL = `polish-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;

test.describe.configure({ mode: "serial" }); // one account for the file

interface Seen {
  draftLengths: number[];   // the draft's text length, each time it changed
  lastDraft: string;
  writing: boolean;         // the progress panel's "Itinerary — Writing" step was shown
  updatingParts: string[];  // every part of the plan that was marked as updating
  sections: string[];       // every value of the itinerary's data-updating
}

/** Start recording what the page goes through. A reload ends the recording. */
async function watch(page: Page) {
  await page.evaluate(() => {
    const seen = { draftLengths: [] as number[], lastDraft: "", writing: false, updatingParts: [] as string[], sections: [] as string[] };
    (window as unknown as { __seen: typeof seen }).__seen = seen;
    const look = () => {
      // the days written so far — not the card's heading, which changes once the reply is complete
      const draft = document.querySelector("[data-live-draft] ol");
      if (draft?.textContent) {
        if (seen.draftLengths[seen.draftLengths.length - 1] !== draft.textContent.length) seen.draftLengths.push(draft.textContent.length);
        seen.lastDraft = draft.textContent;
      }
      if (document.querySelector('[data-agent="itinerary_builder"][data-state="running"]')) seen.writing = true;
      document.querySelectorAll('[data-updating="true"]').forEach((element) => {
        const part = element.getAttribute("data-part");
        if (part && !seen.updatingParts.includes(part)) seen.updatingParts.push(part);
      });
      const section = document.querySelector('[aria-label="Your itinerary"]')?.getAttribute("data-updating");
      if (section && !seen.sections.includes(section)) seen.sections.push(section);
    };
    new MutationObserver(look).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
  });
}

const seen = (page: Page) => page.evaluate(() => (window as unknown as { __seen: Seen }).__seen);

/** The parts of the plan currently marked as changed ("Updated"). */
async function changedParts(page: Page): Promise<string[]> {
  const parts = await page.locator('[data-changed="true"]').evaluateAll((elements) => elements.map((e) => e.getAttribute("data-part") ?? ""));
  return parts.sort();
}

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

/** A three-day trip starting on 1 December + `day`. */
async function createTrip(page: Page, trip: { destination: string; day: number; budget?: string }) {
  const date = (offset: number) => `${YEAR}-12-${String(trip.day + offset).padStart(2, "0")}`;
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill(trip.destination);
  await page.getByLabel("Start date").fill(date(0));
  await page.getByLabel("End date").fill(date(2));
  await page.getByLabel(/budget/i).fill(trip.budget ?? "50000");
  await page.getByLabel("Travellers", { exact: true }).fill("2");
  await page.getByLabel(/interests/i).fill("beach, food");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.locator('[data-stream="connected"]')).toBeAttached(); // pub/sub has no replay
}

async function send(page: Page, text: string) {
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill(text);
  await chat.press("Enter");
}

/** Nothing on the page is wider than the screen: no sideways scrolling. */
async function expectFitsTheScreen(page: Page, where: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, `${where}: the page scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(0);
}

test("the itinerary is written live, and a change animates only what it changes", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);
  await createTrip(page, { destination: "Goa", day: 14 });

  // ── Dev A: token-by-token streaming ──────────────────────────────────────
  await watch(page);
  await send(page, "A relaxed trip with beaches and good food");
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });

  const writing = await seen(page);
  // the draft grew while it was written: many sizes, each larger than the last
  expect(writing.draftLengths.length).toBeGreaterThanOrEqual(3);
  expect(writing.draftLengths).toEqual([...writing.draftLengths].sort((a, b) => a - b));
  // it showed the places as they arrived, in the day-by-day shape of the plan
  expect(writing.lastDraft).toContain("Fort Aguada");
  expect(writing.lastDraft).toContain("Day 2");
  expect(writing.writing).toBe(true); // and the progress panel had a fourth step for it
  // then the checked and saved itinerary took its place
  await expect(page.locator("[data-live-draft]")).toHaveCount(0);
  await expect(itinerary.getByRole("article")).toHaveCount(3);

  // ── Dev B: after the itinerary, the composer says what a message will do ──
  await expect(page.getByText("Refine this trip:", { exact: true })).toBeVisible();

  // ── A targeted change: only the stay moves ───────────────────────────────
  await watch(page);
  await send(page, "Change hotels to something closer to the beach");
  const changes = page.getByRole("list", { name: "What changed" });
  await expect(changes).toBeVisible({ timeout: 90_000 });
  await expect(changes.getByRole("listitem")).toHaveText(["Stay: Goa Grand → Baga Beach House (same price, ₹4,500 a night)"]);

  const refining = await seen(page);
  // while it ran, the stay rows and the stay tile were the only parts marked as updating…
  expect(refining.updatingParts.sort()).toEqual(["stay:1", "stay:2", "tile:stay"]);
  // …and the plan as a whole never was: no dimming, no "everything is changing"
  expect(refining.sections.filter((section) => section !== "pending" && section !== "stay")).toEqual([]);

  // ── Visual diff: what came back different is marked, briefly ─────────────
  expect(await changedParts(page)).toEqual(["stay:1", "stay:2", "tile:stay"]);
  await expect(itinerary.getByText("Updated", { exact: true })).toHaveCount(3);
  for (const part of ["flight", "stop:1-morning", "stop:1-afternoon", "stop:2-morning", "tile:flights", "tile:activities", "total"]) {
    await expect(page.locator(`[data-part="${part}"]`)).not.toHaveAttribute("data-changed", "true");
  }
  // the marks fade: a few seconds later the plan is just the plan again
  await expect(page.locator('[data-changed="true"]')).toHaveCount(0, { timeout: 10_000 });
  await expect(itinerary.getByRole("article").first()).toContainText("Baga Beach House");
});

test("a search that failed says why, and Retry runs only that search", async ({ page }) => {
  await signIn(page);
  await createTrip(page, { destination: "Hampi", day: 18 }); // the hotel search is down until retried
  await send(page, "Temples and boulders");
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });

  // the plan was built without a stay, and the progress says which search failed and why
  const progress = page.getByLabel("Planning progress");
  const hotel = progress.locator('[data-agent="hotel_agent"]');
  await expect(hotel).toHaveAttribute("data-state", "failed");
  await expect(hotel).toContainText("The hotel search is not answering (HTTP 503).");
  await expect(page.getByLabel("Trip cost").getByText("Not included")).toBeVisible();

  // a reloaded page still knows — GET /status says why a search failed
  await page.reload();
  await expect(hotel).toHaveAttribute("data-state", "failed");
  await expect(hotel).toContainText("The hotel search is not answering (HTTP 503).");
  const retry = progress.getByRole("button", { name: "Retry the hotel search" });
  await expect(retry).toBeVisible();
  await expect(progress.getByRole("button", { name: /retry the (flight|activities) search/i })).toHaveCount(0);

  await expect(page.locator('[data-stream="connected"]')).toBeAttached();
  await watch(page);
  await retry.click();
  await expect(page.getByText("Done. The total is now ₹17,200 — ₹9,000 more than before.")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("list", { name: "What changed" }).last()).toContainText("Stay added: Goa Grand, ₹4,500 a night");

  // only the stay was worked on, and the plan gained it without losing anything
  expect((await seen(page)).updatingParts).toEqual(["tile:stay"]);
  expect(await changedParts(page)).toEqual(["stay:1", "stay:2", "tile:stay", "total"]);
  await expect(itinerary.getByRole("article").first()).toContainText("Fort Aguada");
  await expect(hotel).toHaveAttribute("data-state", "completed");
  await expect(progress.getByRole("button", { name: /retry/i })).toHaveCount(0);
});

test("a search that would fail the same way again says what would help, and offers no Retry", async ({ page }) => {
  // Found on a live run: a destination typed in Devanagari is under no airport, and the panel showed
  // the tool's words ("add the city to _CITY_IATA in tools.py") beside a Retry that could only fail again.
  await signIn(page);
  await createTrip(page, { destination: "Kaza", day: 20 });
  await send(page, "Monasteries and high passes");
  await expect(page.getByLabel("Your itinerary").getByRole("article").first()).toBeVisible({ timeout: 90_000 });

  const progress = page.getByLabel("Planning progress");
  const flights = progress.locator('[data-agent="flight_agent"]');
  await expect(flights).toHaveAttribute("data-state", "failed");
  await expect(flights).toContainText("No airport is known by this name. Write the destination in English");
  await expect(flights).not.toContainText("_CITY_IATA");
  await expect(progress.getByRole("button", { name: /retry/i })).toHaveCount(0);

  // the same after a reload: GET /status says it, and that a retry would not help
  await page.reload();
  await expect(flights).toHaveAttribute("data-state", "failed");
  await expect(flights).toContainText("No airport is known by this name.");
  await expect(progress.getByRole("button", { name: /retry/i })).toHaveCount(0);
});

test("a run that fails outright says what failed, and Retry plans it again", async ({ page }) => {
  await signIn(page);
  await createTrip(page, { destination: "Shimla", day: 22 }); // every search is down until the trip is retried
  await send(page, "Snow and long walks");

  const failed = page.getByLabel("Planning failed");
  await expect(failed).toBeVisible({ timeout: 90_000 });
  await expect(failed).toContainText("No flights, places to stay or things to do were found for Shimla");
  const searches = failed.getByRole("list", { name: "Searches that failed" }).getByRole("listitem");
  await expect(searches).toHaveCount(3);
  await expect(searches.first()).toContainText("Flights");
  await expect(searches.first()).toContainText("The flight search is not answering (HTTP 503).");

  // the same after a reload: the reason and each search's own error come back from GET /status
  await page.reload();
  await expect(failed).toContainText("No flights, places to stay or things to do were found for Shimla");
  await expect(searches).toHaveCount(3);
  await expect(page.locator('[data-stream="connected"]')).toBeAttached();

  await failed.getByRole("button", { name: "Retry" }).click();
  // planned again from what was asked the first time — and this time the searches answer
  await expect(page.getByLabel("Your itinerary").getByRole("article").first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(/your itinerary is ready/i)).toBeVisible();
  await expect(failed).toHaveCount(0);
});

test("every view works at 375px, and the planning progress is a bottom sheet there", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });

  await page.goto("/login");
  await expectFitsTheScreen(page, "sign-in");
  await signIn(page);
  await expectFitsTheScreen(page, "trips list");
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await expectFitsTheScreen(page, "new-trip form");
  await page.getByRole("button", { name: "Cancel" }).click();

  // ── a plan, start to finish ──────────────────────────────────────────────
  await createTrip(page, { destination: "Goa", day: 26 });
  await expectFitsTheScreen(page, "a trip with nothing planned");
  await send(page, "A relaxed trip with beaches and good food");

  const sheet = page.locator("[data-progress-sheet]");
  await expect(sheet).toBeVisible();
  const progress = page.getByLabel("Planning progress");
  const toggle = sheet.getByRole("button").first();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click(); // the sheet opens to the full progress panel
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(progress).toBeVisible();
  await expectFitsTheScreen(page, "planning");

  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });
  await expect(sheet).toHaveCount(0); // nothing left to report: the sheet goes
  await expect(progress).toHaveCount(0); // and on a phone it is not repeated inside the page
  await expectFitsTheScreen(page, "the itinerary");
  await page.getByLabel("Trip map").scrollIntoViewIfNeeded();
  await expectFitsTheScreen(page, "the map");

  // a change on a phone: the sheet comes back while it runs
  await page.getByRole("textbox", { name: /message the trip assistant/i }).scrollIntoViewIfNeeded();
  await expect(page.getByText("Refine this trip:", { exact: true })).toBeVisible();
  await send(page, "Change hotels to something closer to the beach");
  await expect(sheet).toBeVisible();
  await expect(page.getByRole("list", { name: "What changed" })).toBeVisible({ timeout: 90_000 });
  await expect(sheet).toHaveCount(0);

  // ── a search that failed: the sheet stays, with the way to retry it ──────
  await page.goto("/trips");
  await createTrip(page, { destination: "Badami", day: 26 });
  await send(page, "Caves and temples");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });
  await expect(sheet).toBeVisible();
  await expect(sheet).toContainText("The hotel search failed");
  await sheet.getByRole("button").first().click();
  await expect(progress.locator('[data-agent="hotel_agent"]')).toContainText("The hotel search is not answering (HTTP 503).");
  await expectFitsTheScreen(page, "a search that failed");
  await progress.getByRole("button", { name: "Retry the hotel search" }).click();
  await expect(page.getByText(/Done\. The total is now/)).toBeVisible({ timeout: 90_000 });
  await expect(sheet).toHaveCount(0);

  // ── a run that failed outright ───────────────────────────────────────────
  await page.goto("/trips");
  await createTrip(page, { destination: "Manali", day: 26 });
  await send(page, "Snow and long walks");
  const failed = page.getByLabel("Planning failed");
  await expect(failed).toBeVisible({ timeout: 90_000 });
  await expect(sheet).toHaveCount(0); // the failure is the page here; nothing to tuck into a sheet
  await expectFitsTheScreen(page, "a run that failed");
  await failed.getByRole("button", { name: "Retry" }).click();
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });

  // ── a budget conflict: the priced ways out are cards, one under another ───
  await page.goto("/trips");
  await createTrip(page, { destination: "Udaipur", day: 26, budget: "12000" });
  await send(page, "A long weekend of palaces and lakes");
  const options = page.getByLabel("Budget options");
  await expect(options.locator("[data-alternative]")).toHaveCount(2, { timeout: 90_000 });
  const boxes = await options.locator("[data-alternative]").evaluateAll((cards) => cards.map((card) => card.getBoundingClientRect().toJSON()));
  expect(boxes[1].top).toBeGreaterThan(boxes[0].bottom - 1); // stacked, not squeezed side by side
  for (const box of boxes) expect(box.width).toBeGreaterThan(250); // each one easy to tap
  await expectFitsTheScreen(page, "a budget conflict");

  await page.goto("/trips");
  await expect(page.getByRole("link", { name: /Udaipur/ })).toBeVisible();
  await expectFitsTheScreen(page, "trips list with trips");
});
