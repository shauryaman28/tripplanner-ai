/**
 * Phase 36 — a plan in Hindi, Tamil or Bengali, in the browser.
 *
 * Roadmap acceptance:
 *   - a request in Hindi ("गोवा में 5 दिन की यात्रा, बजट ₹50,000") comes back as a plan in Hindi
 *   - English-input users see zero change
 *   - preferred_language persists across sessions
 *
 * The plan's own words — a stop's local name and description, the local tips — are in its language
 * and marked with its `lang`; the page around them stays English, and so do the names the plan goes
 * by. Runs on the Playwright stack's stub backend (tests/e2e/stub_backend.py): its stand-in translator
 * puts the roadmap's request into English and writes "हिन्दी: …" for whatever it puts into Hindi, and
 * its stand-in builder writes "घूमने की जगह: <place>" as a stop's description.
 */

import { expect, test, type Page } from "@playwright/test";

import { PLAN_LANGUAGES, planLanguage } from "../src/lib/language";

const EMAIL = `language-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;
const ACCEPTANCE = "गोवा में 5 दिन की यात्रा, बजट ₹50,000";

test.describe.configure({ mode: "serial" }); // one account for the file

test("the three languages, and English, which is no language to mark", () => {
  expect(PLAN_LANGUAGES.map((language) => language.code)).toEqual(["hi", "ta", "bn"]);
  expect(planLanguage("hi")?.native).toBe("हिन्दी");
  expect(planLanguage("en")).toBeNull();
  expect(planLanguage(undefined)).toBeNull();
  expect(planLanguage("mr")).toBeNull();
});

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

const saved = (page: Page) =>
  page.waitForResponse((r) => r.url().endsWith("/api/users/preferences") && r.request().method() === "PUT" && r.status() === 200);

test("the plan language is chosen in the header, and is still chosen after signing out and in again", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  const menu = page.getByLabel("Plan language");
  await expect(menu).toHaveValue(""); // none chosen: "As I write"
  await expect(menu.locator("option:checked")).toHaveText("As I write");

  const put = saved(page);
  await menu.selectOption("hi");
  expect((await (await put).json()).preferred_language).toBe("hi");

  await page.reload();
  await expect(page.getByLabel("Plan language")).toHaveValue("hi"); // read back from the server

  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page).toHaveURL(/\/login$/);
  await signIn(page); // a new sign-in, a new session
  await expect(page.getByLabel("Plan language")).toHaveValue("hi");

  const back = saved(page);
  await page.getByLabel("Plan language").selectOption("");
  expect((await (await back).json()).preferred_language).toBeNull();
});

async function planFromChat(page: Page, destination: string, day: string, request: string) {
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill(destination);
  await page.getByLabel("Start date").fill(`${YEAR}-11-${day}`);
  await page.getByLabel("End date").fill(`${YEAR}-11-${String(Number(day) + 2).padStart(2, "0")}`); // 3 days
  await page.getByLabel(/budget/i).fill("50000");
  await page.getByLabel(/interests/i).fill("history");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();

  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill(request);
  await chat.press("Enter");
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").getByText("Day 1", { exact: true })).toBeVisible({ timeout: 90_000 });
  return itinerary;
}

test("a request in Hindi comes back as a plan in Hindi, with the names it goes by in English", async ({ page }) => {
  await signIn(page);
  const itinerary = await planFromChat(page, "Goa", "03", ACCEPTANCE);

  await expect(itinerary.locator('[data-plan-written-in="hi"]')).toContainText("हिन्दी");
  const first = itinerary.getByRole("article").first();
  await expect(first).toContainText("Fort Aguada"); // the name on the map, and in the booking
  const local = first.locator("[data-local-text]").first();
  await expect(local).toHaveAttribute("lang", "hi");
  await expect(local).toContainText("घूमने की जगह: Fort Aguada");

  // a free day says what the page says of one — in Hindi
  const freeDay = itinerary.getByRole("article").nth(2);
  await expect(freeDay.getByText("Free time")).toBeVisible();
  await expect(freeDay.locator('[data-local-text][lang="hi"]')).toContainText("हिन्दी: Nothing booked");

  // the local tips are in Hindi; their headings, the page's own words, are not
  await page.getByRole("button", { name: /getting around/i }).click();
  const transport = page.getByRole("region", { name: /getting around/i });
  await expect(transport).toHaveAttribute("lang", "hi");
  await expect(transport).toContainText("हिन्दी: Rent a scooter");
});

test("a request in English is planned as it always was: nothing in another language", async ({ page }) => {
  await signIn(page);
  const itinerary = await planFromChat(page, "Goa", "13", "A relaxed long weekend, forts and food");
  await expect(itinerary.getByRole("article").first()).toContainText("Fort Aguada");
  await expect(itinerary.locator("[data-local-text]")).toHaveCount(0);
  await expect(itinerary.locator("[data-plan-written-in]")).toHaveCount(0);
  await expect(itinerary.getByRole("article").nth(2).getByText("Nothing booked — explore the area at your own pace.")).toBeVisible();
});
