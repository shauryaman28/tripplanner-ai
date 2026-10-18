/**
 * Phase 38 — a day's weather, on the page.
 *
 * Roadmap acceptance (Dev B): the weather icon renders for all three condition types — sunny, partly
 * cloudy, rainy; the "(climate estimate)" badge is on day 6 and after. First the words (lib/weather.ts),
 * then a week in Kodaikanal on the Playwright stack's stub backend (tests/e2e/stub_backend.py): its
 * stand-in forecast has rain on day 2, clouds on day 3, sun otherwise — and, past five days of forecast,
 * the climate estimate, as the real weather tool gives past OpenWeatherMap's five days.
 */

import { expect, test } from "@playwright/test";

import { WEATHER_LABELS, weatherWords } from "../src/lib/weather";

test("a day's weather in words, and none for a day without any", () => {
  expect(weatherWords({ condition: "Rain", kind: "rainy", temp_high_c: 28.6, temp_low_c: 23.1, estimate: false })).toBe(
    "Rain · 29° / 23°",
  );
  expect(weatherWords({ condition: "Clouds", kind: "cloudy", temp_high_c: 30, temp_low_c: 22, estimate: false })).toBe(
    "Partly cloudy · 30° / 22°",
  );
  expect(weatherWords({ condition: "Sunny", kind: "sunny", estimate: true })).toBe("Sunny");
  expect(weatherWords(null)).toBeNull();
  expect(Object.keys(WEATHER_LABELS)).toEqual(["sunny", "cloudy", "rainy", "snowy"]);
});

const YEAR = new Date().getFullYear() + 1;

test("each day says its weather, and the days past the forecast say they are an estimate", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(`weather-${Date.now()}@example.com`);
  await page.getByLabel("Password", { exact: true }).fill("test-password-123");
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill("Kodaikanal");
  await page.getByLabel("Start date").fill(`${YEAR}-09-01`);
  await page.getByLabel("End date").fill(`${YEAR}-09-07`); // seven days
  await page.getByLabel(/budget/i).fill("80000");
  await page.getByLabel(/interests/i).fill("history");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("A quiet week in the hills");
  await chat.press("Enter");

  const days = page.getByLabel("Your itinerary").getByRole("article");
  await expect(days.getByText("Day 1", { exact: true })).toBeVisible({ timeout: 90_000 });
  await expect(days).toHaveCount(7);

  // the three kinds, each with its own icon
  const kinds = ["sunny", "rainy", "cloudy", "sunny", "sunny", "sunny", "sunny"];
  for (let n = 0; n < kinds.length; n++) {
    await expect(days.nth(n).locator("[data-weather]")).toHaveAttribute("data-weather", kinds[n]);
  }
  await expect(days.nth(0).locator("[data-weather] svg.lucide-sun")).toBeVisible();
  await expect(days.nth(1).locator("[data-weather] svg.lucide-cloud-rain")).toBeVisible();
  await expect(days.nth(2).locator("[data-weather] svg.lucide-cloud-sun")).toBeVisible();
  await expect(days.nth(1).locator("[data-weather]")).toContainText("Rain · 30° / 22°");
  await expect(days.nth(2).locator("[data-weather]")).toContainText("Partly cloudy");

  // the badge: on days 6 and 7, and on no other
  for (const n of [0, 1, 2, 3, 4]) await expect(days.nth(n).locator("[data-climate-estimate]")).toHaveCount(0);
  for (const n of [5, 6]) await expect(days.nth(n).locator("[data-climate-estimate]")).toHaveText("climate estimate");
});
