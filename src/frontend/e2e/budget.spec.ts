/**
 * Phase 21 — a budget conflict, priced, in the browser.
 *
 * Roadmap acceptance: the budget conflict shows three alternatives with
 * concrete amounts, generated without searching again, tested against a known
 * conflict. The known conflict is the stub backend's Jaisalmer: ₹30,000 direct
 * flights on a ₹42,000 budget for two, three days in December — the flights are
 * 71% of the budget, and at typical prices the trip as asked comes to about
 * ₹46,200 (tests/e2e/stub_backend.py).
 *
 * Each test plans on dates of its own, so the stub's per-search counters of one
 * test never reach another.
 */

import { expect, test, type Page } from "@playwright/test";

const EMAIL = `budget-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;

test.describe.configure({ mode: "serial" }); // one account for the file

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

/** Three days in Jaisalmer from 1 December + `day`, for two, on ₹42,000 — then describe it to the assistant. */
async function conflictInJaisalmer(page: Page, day: number) {
  const date = (offset: number) => `${YEAR}-12-${String(day + offset).padStart(2, "0")}`;
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill("Jaisalmer");
  await page.getByLabel("Start date").fill(date(0));
  await page.getByLabel("End date").fill(date(2));
  await page.getByLabel(/budget/i).fill("42000");
  await page.getByLabel("Travellers", { exact: true }).fill("2");
  await page.getByLabel(/interests/i).fill("forts, desert");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.locator('[data-stream="connected"]')).toBeAttached(); // pub/sub has no replay

  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("Forts, dunes and a camel safari");
  await chat.press("Enter");
  const options = page.getByLabel("Budget options");
  await expect(options.getByText("Over budget", { exact: true })).toBeVisible({ timeout: 90_000 });
  return options;
}

test("a budget conflict prices three ways out, and a shorter trip goes ahead on the same flights", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  const options = await conflictInJaisalmer(page, 4);

  // the trip as asked, priced: what the three amounts stand against
  const estimate = options.locator("[data-estimate]");
  await expect(estimate).toContainText("With a 4-star hotel and things to do, the trip as asked comes to about ₹46,200");
  await expect(estimate).toContainText("likely between ₹37,000 and ₹55,400 once booked");
  await expect(estimate).toContainText("December is peak season in Jaisalmer: prices run about 35% above the off-season.");
  await expect(options.getByText("Ways to bring it within ₹42,000")).toBeVisible();

  // three alternatives, each with what it would come to — said in words, not only by colour
  const cards = options.locator("[data-alternative]");
  await expect(cards).toHaveCount(3);
  await expect(cards.nth(0)).toContainText("Stay at a 3-star hotel");
  await expect(cards.nth(0)).toContainText("About ₹40,800");
  await expect(cards.nth(0)).toContainText("Within your budget");
  await expect(cards.nth(0)).toContainText("About ₹5,400 less than as asked");
  await expect(cards.nth(1)).toContainText("Make it 2 days instead of 3");
  await expect(cards.nth(1)).toContainText("About ₹38,800");
  await expect(cards.nth(2)).toContainText(/Go in \w+ instead — the summer heat/);
  await expect(cards.nth(2)).toContainText("About ₹34,200");
  await expect(cards.nth(2)).toContainText("Flights about ₹7,800 less");
  // and the two plain actions
  await expect(options.getByRole("button", { name: "Search for cheaper connecting flights" })).toBeVisible();
  await expect(options.getByRole("button", { name: "Increase total budget to ₹60,000" })).toBeVisible();

  // the assistant says the price too
  await expect(page.getByText(/the trip as asked comes to about ₹46,200\. Pick one of the options/)).toBeVisible();

  // a reload: the event that carried them is gone — GET /status brings back the priced trip and its options
  await page.reload();
  await expect(cards).toHaveCount(3);
  await expect(estimate).toContainText("₹46,200");
  await expect(page.locator('[data-stream="connected"]')).toBeAttached();

  // the shorter trip: the same ₹30,000 flights, and the check goes ahead instead of stopping again
  await cards.nth(1).click();
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });
  await expect(itinerary.getByRole("article")).toHaveCount(2);
  await expect(page.getByText(`4 – 5 Dec ${YEAR} · 1 night`)).toBeVisible();
  await expect(options).toHaveCount(0);
  await expect(page.getByLabel("Trip cost").getByText(/under budget/i)).toBeVisible();
});

test("a cheaper stay goes ahead on the same flights, for the whole trip", async ({ page }) => {
  await signIn(page);
  const options = await conflictInJaisalmer(page, 8);
  await options.locator('[data-alternative="cheaper_hotel"]').click();

  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });
  await expect(itinerary.getByRole("article")).toHaveCount(3); // the trip as asked: three days
  await expect(page.getByText(`8 – 10 Dec ${YEAR} · 2 nights`)).toBeVisible();
  await expect(options).toHaveCount(0);
});

test("the off-season moves the trip to its dates — and there it fits", async ({ page }) => {
  await signIn(page);
  const options = await conflictInJaisalmer(page, 12);
  const offSeason = options.locator('[data-alternative="off_peak"]');
  const month = /Go in (\w+) instead/.exec((await offSeason.textContent()) ?? "")?.[1] ?? "";
  expect(month).not.toBe("");
  await offSeason.click();

  // The stub's fare does not change with the season: the same ₹30,000 is still 71% of the budget. But at
  // off-season prices a 4-star stay and things to do bring the trip to ₹42,000 — it fits, so no conflict.
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(new RegExp(`^12 – 14 ${month.slice(0, 3)} \\d{4} · 2 nights$`))).toBeVisible();
  await expect(options).toHaveCount(0);
  await expect(page.getByLabel("Trip cost").getByText(/of the budget left/)).toBeVisible();
});
