/**
 * Phase 22 — local tips.
 *
 * Roadmap acceptance:
 *   - "Local Tips" renders for a destination the agent returned data for
 *   - it is gracefully absent when the agent failed — no error shown
 *   - the accordion opens and closes; every section is closed until asked for
 *
 * First the pure functions that read `structured_data.local_intelligence`
 * (lib/tips.ts), then the page, on the Playwright stack's stub backend
 * (tests/e2e/stub_backend.py). Its stand-in model has tips for everywhere but
 * Pondicherry (it never answers) and Gokarna (it answers the second time).
 */

import { expect, test, type Page } from "@playwright/test";

import { describeChanges } from "../src/lib/changes";
import { hasTips, samePlace, tipSections, whereInPlan, TIP_SECTIONS } from "../src/lib/tips";
import type { ActivitySlot, DaySchedule, StructuredItinerary } from "../src/lib/types";

// ── Reading the tips ────────────────────────────────────────────────────────

const stop = (activity: string): ActivitySlot => ({ activity, cost: 0 });
const day = (n: number, slots: Partial<Pick<DaySchedule, "morning" | "afternoon" | "evening">>): DaySchedule => ({
  day: n,
  date: `2026-12-${9 + n}`,
  morning: null,
  afternoon: null,
  evening: null,
  hotel: null,
  flight: null,
  ...slots,
});
const DAYS = [
  day(1, { morning: stop("Fort Aguada"), afternoon: stop("Baga Beach") }),
  day(2, { morning: stop("Explore the area"), evening: stop("Basilica of Bom Jesus") }),
];
const TIPS = {
  local_transport: "Rent a scooter.",
  cultural_norms: ["Dress modestly in churches."],
  tourist_traps: ["Agree the taxi fare first.", "Skip the ten-language menus."],
  best_times: { "Anjuna Flea Market": "Wednesday afternoon.", "Bom Jesus Basilica": "Before the tour buses, at 9." },
  safety_tips: ["Keep valuables off the beach."],
};

test("the five sections, in order, each with what it says", () => {
  const sections = tipSections(TIPS, DAYS);
  expect(sections.map((section) => section.label)).toEqual(["Getting around", "Local customs", "Tourist traps", "Best times to visit", "Staying safe"]);
  expect(sections.map((section) => section.key)).toEqual(TIP_SECTIONS.map((section) => section.key));
  expect(sections[0].tips).toEqual([{ text: "Rent a scooter." }]);
  expect(sections[2].tips.map((tip) => tip.text)).toEqual(["Agree the taxi fare first.", "Skip the ten-language menus."]);
});

test("a place the plan visits is marked with its day, and listed first", () => {
  const places = tipSections(TIPS, DAYS)[3].tips;
  expect(places).toEqual([
    { text: "Before the tour buses, at 9.", place: "Bom Jesus Basilica", inPlan: "Day 2 · Evening" },
    { text: "Wednesday afternoon.", place: "Anjuna Flea Market", inPlan: undefined },
  ]);
});

test("a section with nothing to say is left out, and no tips at all is no sections", () => {
  expect(tipSections({ local_transport: "Walk.", cultural_norms: [], best_times: {} }).map((s) => s.key)).toEqual(["local_transport"]);
  for (const nothing of [undefined, null, "tips", 7, [], {}, { cultural_norms: [] }, { local_transport: "  " }]) {
    expect(tipSections(nothing)).toEqual([]);
    expect(hasTips(nothing)).toBe(false);
  }
  expect(hasTips(TIPS)).toBe(true);
});

test("anything that is not text is left out", () => {
  const odd = {
    local_transport: ["a", "list"],
    cultural_norms: ["Shoes off.", 7, null, "", { tip: "x" }],
    tourist_traps: "a string where a list should be",
    best_times: { "Fort Aguada": ["morning"], "Baga Beach": " At sunset. ", "": "never" },
    safety_tips: null,
  };
  expect(tipSections(odd, DAYS)).toEqual([
    { key: "cultural_norms", label: "Local customs", tips: [{ text: "Shoes off." }] },
    { key: "best_times", label: "Best times to visit", tips: [{ text: "At sunset.", place: "Baga Beach", inPlan: "Day 1 · Afternoon" }] },
  ]);
});

test("the same place under a slightly different name is recognised; a different place is not", () => {
  for (const [a, b] of [
    ["Fort Aguada", "Aguada Fort"],
    ["Fort Aguada", "fort aguada, Goa"],
    ["Basilica of Bom Jesus", "Bom Jesus Basilica"],
    ["São Tomé Church", "Sao Tome church"],
    ["काशी विश्वनाथ मंदिर", "काशी विश्वनाथ मंदिर"],
  ]) {
    expect(samePlace(a, b), `${a} / ${b}`).toBe(true);
  }
  for (const [a, b] of [
    ["Fort", "Fort Aguada"], // one common word is not a name
    ["Baga Beach", "Calangute Beach"],
    ["काशी", "कोशी"], // a vowel sign is not an accent
    ["", "Fort Aguada"],
    ["—", "Fort Aguada"],
  ]) {
    expect(samePlace(a, b), `${a} / ${b}`).toBe(false);
  }
  expect(whereInPlan("Explore the area", DAYS)).toBeUndefined(); // free time is not a place
  expect(whereInPlan("Baga beach", DAYS)).toBe("Day 1 · Afternoon");
});

test("the assistant says when a change brought local tips, and only then", () => {
  const before: StructuredItinerary = { days: DAYS, total_cost: 9000, currency: "INR", local_intelligence: null };
  const withTips: StructuredItinerary = { ...before, local_intelligence: TIPS };
  expect(describeChanges(before, withTips).points).toEqual(["Local tips added"]);
  expect(describeChanges(before, withTips).changed).toBe(true);
  expect(describeChanges(withTips, structuredClone(withTips)).points).toEqual([]);
  expect(describeChanges({ days: DAYS, total_cost: 9000, currency: "INR" }, before).changed).toBe(false); // none before, none after
});

// ── On the page ─────────────────────────────────────────────────────────────

const EMAIL = `tips-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;

test.describe.configure({ mode: "serial" }); // one account for the file

async function signIn(page: Page, register = false) {
  await page.goto("/login");
  if (register) await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: register ? /create account/i : "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

/** Create a three-day trip starting on `day` October and plan it. Resolves when the day cards are on the page. */
async function planTrip(page: Page, destination: string, day: number) {
  const date = (offset: number) => `${YEAR}-10-${String(day + offset).padStart(2, "0")}`;
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill(destination);
  await page.getByLabel("Start date").fill(date(0));
  await page.getByLabel("End date").fill(date(2));
  await page.getByLabel(/budget/i).fill("50000");
  await page.getByLabel("Travellers", { exact: true }).fill("2");
  await page.getByLabel(/interests/i).fill("beach, food");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.locator('[data-stream="connected"]')).toBeAttached(); // pub/sub has no replay
  await send(page, "Beaches, forts and good food");
  await expect(page.getByLabel("Your itinerary").getByRole("article")).toHaveCount(3, { timeout: 90_000 });
}

async function send(page: Page, text: string) {
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill(text);
  await chat.press("Enter");
}

test("local tips sit under the day cards, closed until asked for, and open and close", async ({ page }) => {
  await signIn(page, true);
  await planTrip(page, "Goa", 5);

  const tips = page.getByRole("region", { name: "Local tips", exact: true });
  await expect(tips).toBeVisible();
  await expect(tips).toContainText("General advice from the assistant's own knowledge of Goa, not from a live source.");

  // below the day-by-day cards, above the map
  const top = (locator: ReturnType<Page["locator"]>) => locator.evaluate((element) => element.getBoundingClientRect().top);
  const lastDay = page.getByLabel("Your itinerary").getByRole("article").last();
  expect(await top(tips)).toBeGreaterThan(await top(lastDay));
  expect(await top(page.locator("#trip-map"))).toBeGreaterThan(await top(tips));

  // ── five sections, every one collapsed ────────────────────────────────────
  const headings = tips.getByRole("button");
  await expect(headings).toHaveText([/^Getting around$/, /^Local customs2 tips$/, /^Tourist traps2 tips$/, /^Best times to visit2 places$/, /^Staying safe5 tips$/]);
  for (const heading of await headings.all()) await expect(heading).toHaveAttribute("aria-expanded", "false");
  await expect(tips.getByText("Rent a scooter for about ₹400 a day")).toBeHidden();
  await expect(tips.getByText("Agree the taxi fare before you get in")).toBeHidden();

  // ── a click opens a section — only that one — and another click closes it ──
  const transport = tips.getByRole("button", { name: "Getting around" });
  await transport.click();
  await expect(transport).toHaveAttribute("aria-expanded", "true");
  const panel = tips.getByRole("region", { name: "Getting around" });
  await expect(panel).toBeVisible();
  await expect(panel).toHaveText("Rent a scooter for about ₹400 a day — the beaches are too far apart to walk between.");
  await expect(tips.getByText("Agree the taxi fare before you get in")).toBeHidden();
  await transport.click();
  await expect(transport).toHaveAttribute("aria-expanded", "false");
  await expect(panel).toBeHidden();

  // ── by keyboard too, and two can be open at once ──────────────────────────
  const traps = tips.getByRole("button", { name: /Tourist traps/ });
  await traps.focus();
  await page.keyboard.press("Enter");
  await expect(traps).toHaveAttribute("aria-expanded", "true");
  await expect(tips.getByRole("region", { name: /Tourist traps/ }).getByRole("listitem")).toHaveText([
    "Skip the restaurants with menus in ten languages near Calangute; eat where the taxi drivers eat.",
    "Agree the taxi fare before you get in — there are no meters.",
  ]);
  const customs = tips.getByRole("button", { name: /Local customs/ });
  await customs.focus();
  await page.keyboard.press("Space");
  await expect(customs).toHaveAttribute("aria-expanded", "true");
  await expect(traps).toHaveAttribute("aria-expanded", "true");
  // what the model wrote is shown as plain text: its markdown is gone, its made-up key is nowhere
  await expect(tips.getByRole("region", { name: /Local customs/ }).getByRole("listitem").first()).toHaveText(
    "Dress modestly in churches and temples: shoulders and knees covered.",
  );
  await expect(tips).not.toContainText("**");
  await expect(tips).not.toContainText("nobody");

  // ── best times: the place the plan visits comes first, with where it is in the plan ──
  await tips.getByRole("button", { name: /Best times to visit/ }).click();
  const places = tips.getByRole("region", { name: /Best times to visit/ }).getByRole("listitem");
  await expect(places).toHaveCount(2);
  await expect(places.first()).toContainText("Fort Aguada");
  await expect(places.first()).toContainText("In your plan · Day 1 · Morning");
  await expect(places.first()).toContainText("Early morning, before 9 am");
  await expect(places.last()).toContainText("Anjuna Flea Market");
  await expect(places.last()).not.toContainText("In your plan");

  // ── a list longer than asked for is cut to five ───────────────────────────
  await tips.getByRole("button", { name: /Staying safe/ }).click();
  await expect(tips.getByRole("region", { name: /Staying safe/ }).getByRole("listitem")).toHaveCount(5);

  // ── they are part of the saved plan: still there after a reload, closed again ──
  await page.reload();
  await expect(tips).toBeVisible();
  for (const heading of await headings.all()) await expect(heading).toHaveAttribute("aria-expanded", "false");

  // ── and a change to the plan keeps them ───────────────────────────────────
  await expect(page.locator('[data-stream="connected"]')).toBeAttached();
  await send(page, "Change hotels to something closer to the beach");
  const changes = page.getByRole("list", { name: "What changed" });
  await expect(changes.getByRole("listitem")).toHaveText(["Stay: Goa Grand → Baga Beach House (same price, ₹4,500 a night)"], { timeout: 90_000 });
  await expect(tips.getByRole("button")).toHaveCount(5);
});

test("when the agent has nothing, there is simply no section — and no error", async ({ page }) => {
  await signIn(page);
  await planTrip(page, "Pondicherry", 9); // the stand-in model never answers for it

  // the plan is whole…
  await expect(page.getByText(/your itinerary is ready/i)).toBeVisible();
  await expect(page.getByLabel("Trip cost").getByText("₹17,200")).toBeVisible();
  await expect(page.locator("#trip-map")).toBeVisible();
  // …the tips are just not there…
  await expect(page.getByRole("region", { name: "Local tips", exact: true })).toHaveCount(0);
  await expect(page.getByText("Local tips")).toHaveCount(0);
  // …and nothing anywhere says that anything failed
  const progress = page.getByLabel("Planning progress");
  for (const agent of ["flight_agent", "hotel_agent", "activities_agent"]) {
    await expect(progress.locator(`[data-agent="${agent}"]`)).toHaveAttribute("data-state", "completed");
  }
  await expect(progress.getByRole("button", { name: /retry/i })).toHaveCount(0);
  await expect(page.locator("[data-toast]")).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: /./ })).toHaveCount(0);
  await expect(page.getByText(/failed|went wrong|could not/i)).toHaveCount(0);

  // the same after a reload
  await page.reload();
  await expect(page.getByLabel("Your itinerary").getByRole("article")).toHaveCount(3);
  await expect(page.getByText("Local tips")).toHaveCount(0);
});

test("a plan made without tips gets them with its next change, and the assistant says so", async ({ page }) => {
  await signIn(page);
  await planTrip(page, "Gokarna", 13); // the stand-in model answers the second time it is asked
  const tips = page.getByRole("region", { name: "Local tips", exact: true });
  await expect(tips).toHaveCount(0);

  await send(page, "Change hotels to something closer to the beach");
  const changes = page.getByRole("list", { name: "What changed" });
  await expect(changes.getByRole("listitem")).toHaveText(
    ["Stay: Goa Grand → Baga Beach House (same price, ₹4,500 a night)", "Local tips added"],
    { timeout: 90_000 },
  );
  await expect(tips).toBeVisible();
  await expect(tips).toContainText("knowledge of Gokarna");
  await expect(tips.getByRole("button")).toHaveCount(5);
});

test("local tips fit a phone", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await signIn(page);
  await page.getByRole("link", { name: /Goa/ }).click(); // the trip the first test planned
  const tips = page.getByRole("region", { name: "Local tips", exact: true });
  await expect(tips).toBeVisible();
  for (const heading of await tips.getByRole("button").all()) await heading.click();
  await expect(tips.getByRole("region")).toHaveCount(5);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, `the page scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(0);
  // every heading is a comfortable target for a thumb
  for (const heading of await tips.getByRole("button").all()) {
    expect((await heading.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
});
