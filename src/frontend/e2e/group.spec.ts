/**
 * Phase 25 — a group trip, in the browser.
 *
 * Roadmap acceptance:
 *   - a 4-person group with varied interests gets a plan that balances them: at least one stop per
 *     traveller in every two days, and no one traveller's interests filling it
 *   - the per-person cost is shown prominently in the itinerary header, beside the total
 *
 * Runs on the Playwright stack's stub backend (tests/e2e/stub_backend.py). For "Coorg" its
 * attraction search answers by interest, each place saying what it was found under — and finds
 * nothing for "spa", as the real provider does. The four travellers are the roadmap's.
 */

import { expect, test, type Page } from "@playwright/test";

const EMAIL = `group-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;

const TRAVELLERS = [
  { name: "Asha", interests: "beach, food" },
  { name: "Ben", interests: "history, culture" },
  { name: "Chitra", interests: "adventure" },
  { name: "Dev", interests: "spa, relaxation" },
];

test.describe.configure({ mode: "serial" }); // one account for the file

let groupTrip = ""; // the first test's trip, for the last one to open on a phone

async function register(page: Page) {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

/** The new-trip form filled in for four days in Coorg, up to — not including — who is going. */
async function startTrip(page: Page, day: number) {
  const date = (offset: number) => `${YEAR}-11-${String(day + offset).padStart(2, "0")}`;
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill("Coorg");
  await page.getByLabel("Start date").fill(date(0));
  await page.getByLabel("End date").fill(date(3));
  await page.getByLabel(/budget/i).fill("80000");
  await page.getByLabel("Travellers", { exact: true }).fill("4");
}

async function nameTravellers(page: Page, travellers: { name: string; interests: string }[]) {
  for (let index = 0; index < travellers.length; index++) {
    await page.getByLabel(`Traveller ${index + 1} name`).fill(travellers[index].name);
    await page.getByLabel(`Traveller ${index + 1} interests`).fill(travellers[index].interests);
  }
}

/** "₹21,700" → 21700 */
const rupees = (text: string) => Number(text.replace(/[^\d]/g, ""));

/** Nothing on the page is wider than the screen: no sideways scrolling. */
async function expectFitsTheScreen(page: Page, where: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, `${where}: the page scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(0);
}

test("four travellers who want different things get a plan with something for each, and their share of it", async ({ page }) => {
  await register(page);
  await startTrip(page, 10);

  // who is going: one row per traveller, as many as the form says are travelling
  await page.getByRole("button", { name: /say what each traveller enjoys/i }).click();
  const group = page.getByRole("group", { name: /who is going/i });
  await expect(group.getByRole("textbox", { name: /traveller \d name/i })).toHaveCount(4);
  await expect(page.getByLabel(/^interests/i)).toHaveCount(0); // the shared interests give way to each one's own
  await nameTravellers(page, TRAVELLERS);

  const created = page.waitForResponse((response) => response.url().endsWith("/trips") && response.request().method() === "POST");
  await page.getByRole("button", { name: /create & plan/i }).click();
  const trip = await (await created).json();
  expect(trip.group_size).toBe(4);
  expect(trip.group_members).toEqual(TRAVELLERS.map((t) => ({ name: t.name, interests: t.interests.split(", ") })));
  expect(trip.interests).toEqual(["beach", "food", "history", "culture", "adventure", "spa", "relaxation"]);

  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  groupTrip = page.url();
  await expect(page.locator('[data-stream="connected"]')).toBeAttached(); // pub/sub has no replay
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("A long weekend for the four of us");
  await chat.press("Enter");

  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").first()).toBeVisible({ timeout: 90_000 });

  // ── each traveller's share, beside the total ──
  const cost = page.getByLabel("Trip cost");
  const share = cost.locator('[data-part="per-person"]');
  await expect(share).toContainText("/ person");
  await expect(share).toContainText("shared equally by 4 travellers");
  const total = rupees(await cost.locator('[data-part="total"] > p').nth(1).innerText()); // under "Estimated total"
  const perPerson = rupees((await share.innerText()).split("/")[0]);
  expect(total).toBeGreaterThan(0);
  expect(Math.abs(total / 4 - perPerson)).toBeLessThanOrEqual(1); // the roadmap allows ₹100

  // ── who it is for ──
  const who = page.getByLabel("Who it is for");
  await expect(who.locator("[data-balanced]")).toHaveAttribute("data-balanced", "true");
  await expect(who).toContainText("Something for everyone, every two days");
  for (const traveller of TRAVELLERS) {
    const row = who.locator(`[data-member="${traveller.name}"]`);
    await expect(row).toContainText(/[1-9] stops?/); // nobody has none…
  }
  const stops = await who.locator("[data-member]").evaluateAll((rows) => rows.map((row) => Number(row.textContent?.match(/(\d+) stops?/)?.[1] ?? 0)));
  const allStops = await itinerary.locator("[data-suits]").count();
  expect(Math.max(...stops)).toBeLessThan(allStops); // …and nobody has them all
  // what could not be found is said, not hidden
  await expect(who.locator('[data-member="Dev"]')).toContainText("Nothing was found in Coorg for: spa");
  await expect(who.locator('[data-member="Asha"]')).not.toContainText("Nothing was found");

  // ── every stop says who it is for, and every two days have a stop for each traveller ──
  await expect(itinerary.getByText("For Asha and Dev").first()).toBeVisible(); // a beach: hers, and his idea of a rest
  const days = await itinerary.getByRole("article").evaluateAll((cards) =>
    cards.map((card) => Array.from(card.querySelectorAll("[data-suits]")).flatMap((stop) => (stop.getAttribute("data-suits") ?? "").split("|"))),
  );
  expect(days).toHaveLength(4);
  for (const pair of [days.slice(0, 2), days.slice(2, 4)]) {
    const served = new Set(pair.flat());
    for (const traveller of TRAVELLERS) expect(served, `${traveller.name} in two days`).toContain(traveller.name);
  }

  // ── a reloaded page says the same: it is all in the saved itinerary ──
  await page.reload();
  await expect(page.getByLabel("Trip cost").locator('[data-part="per-person"]')).toContainText("/ person");
  await expect(page.getByLabel("Who it is for").locator("[data-member]")).toHaveCount(4);
  await expect(page.getByLabel("Your itinerary").getByText("For Ben").first()).toBeVisible();
});

test("the form says what a group needs, and goes back to shared interests", async ({ page }) => {
  await signIn(page);
  await startTrip(page, 16);
  await page.getByRole("button", { name: /say what each traveller enjoys/i }).click();

  // one traveller named is not a group
  await nameTravellers(page, TRAVELLERS.slice(0, 1));
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Name at least two travellers" })).toBeVisible();

  // interests without a name
  await page.getByLabel("Traveller 2 interests").fill("history");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Every traveller needs a name" })).toBeVisible();

  // the same name twice
  await page.getByLabel("Traveller 2 name").fill("asha");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Two travellers have the same name" })).toBeVisible();
  await expect(page).toHaveURL(/\/trips$/); // nothing was created

  // a fifth traveller is one more traveller; the rows cannot go below two
  await page.getByRole("button", { name: "Add a traveller" }).click();
  await expect(page.getByLabel("Travellers", { exact: true })).toHaveValue("5");
  await expect(page.getByLabel("Traveller 5 name")).toBeVisible();
  for (const row of [5, 4, 3]) await page.getByRole("button", { name: `Remove traveller ${row}` }).click();
  await expect(page.getByRole("button", { name: "Remove traveller 1" })).toBeDisabled();

  // and back: everyone shares the same interests
  await page.getByRole("button", { name: "Everyone shares the same interests" }).click();
  await expect(page.getByLabel(/^interests/i)).toBeVisible();
  await expect(page.getByRole("group", { name: /who is going/i })).toHaveCount(0);
});

test("a trip whose travellers are not told apart still shows each one's share, and no group panel", async ({ page }) => {
  await signIn(page);
  await startTrip(page, 22);
  await page.getByLabel(/^interests/i).fill("beach, food");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.locator('[data-stream="connected"]')).toBeAttached();
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("Four days, nothing fancy");
  await chat.press("Enter");
  await expect(page.getByLabel("Your itinerary").getByRole("article").first()).toBeVisible({ timeout: 90_000 });

  await expect(page.getByLabel("Trip cost").locator('[data-part="per-person"]')).toContainText("shared equally by 4 travellers");
  await expect(page.getByLabel("Who it is for")).toHaveCount(0);
  await expect(page.getByLabel("Your itinerary").locator("[data-suits]")).toHaveCount(0);
});

test("the group form and the group's plan fit a phone", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 760 });
  await signIn(page);
  await startTrip(page, 26);
  await page.getByRole("button", { name: /say what each traveller enjoys/i }).click();
  await nameTravellers(page, TRAVELLERS);
  await expectFitsTheScreen(page, "the form with four travellers");

  // the first test's trip, on a phone
  await page.goto(groupTrip);
  await expect(page.getByLabel("Who it is for")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Trip cost").locator('[data-part="per-person"]')).toBeVisible();
  await expectFitsTheScreen(page, "the group's plan");
});
