/**
 * Phase 37 — a day's clock, on the page.
 *
 * Roadmap acceptance (Dev A): each activity slot has a start and an end time; activities more than 10 km
 * apart have a travel note. First the pure functions that word them (lib/schedule.ts), then a plan on the
 * Playwright stack's stub backend (tests/e2e/stub_backend.py): it has no Google key, so every journey is an
 * estimate, said as one ("about …"). Its stub hotel is in Calangute; the Basilica of Bom Jesus, in Old Goa,
 * is 17 km from it — the one stop with a note.
 */

import { expect, test } from "@playwright/test";

import { formatMinutes, stopTimes, travelWords } from "../src/lib/schedule";

test("a stop's times, and the words for the way there", () => {
  expect(stopTimes({ activity: "Fort Aguada", cost: 0, start_time: "09:05", end_time: "11:05" })).toBe("09:05–11:05");
  expect(stopTimes({ activity: "Fort Aguada", cost: 0 })).toBeNull(); // an itinerary made before Phase 37
  expect([45, 60, 80, 135].map(formatMinutes)).toEqual(["45 min", "1 h", "1 h 20 min", "2 h 15 min"]);
  expect(travelWords({ from: "the airport", minutes: 46, km: 18.9, mode: "taxi", source: "estimate" })).toBe(
    "Taxi · about 46 min · 18.9 km from the airport",
  );
  expect(travelWords({ from: "Baga Beach", minutes: 75, km: 22.4, mode: "auto-rickshaw", source: "google" })).toBe(
    "Auto-rickshaw · 1 h 15 min · 22.4 km from Baga Beach",
  );
});

const EMAIL = `schedule-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";
const YEAR = new Date().getFullYear() + 1;

test("every stop of a plan says when, and the far one how to get there", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill("Goa");
  await page.getByLabel("Start date").fill(`${YEAR}-10-07`);
  await page.getByLabel("End date").fill(`${YEAR}-10-09`);
  await page.getByLabel(/budget/i).fill("50000");
  await page.getByLabel(/interests/i).fill("history");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("A relaxed long weekend");
  await chat.press("Enter");

  const itinerary = page.getByLabel("Your itinerary");
  const days = itinerary.getByRole("article");
  await expect(days.getByText("Day 1", { exact: true })).toBeVisible({ timeout: 90_000 });

  // every stop's eyebrow has its times
  const stops = itinerary.locator("[data-part^='stop:']");
  await expect(stops.first()).toBeVisible();
  for (const eyebrow of await stops.locator(".eyebrow").allTextContents()) {
    expect(eyebrow).toMatch(/^(Morning|Afternoon|Evening) · \d\d:\d\d–\d\d:\d\d|^All day/);
  }
  await expect(days.first().locator(".eyebrow", { hasText: /^Morning · / })).toHaveText(/Morning · 08:\d\d–1[01]:\d\d/);

  // the Basilica is 17 km from the hotel: its way there is a row of its own, an estimate said as one
  const travel = itinerary.locator("[data-travel]");
  await expect(travel).toHaveCount(1);
  await expect(travel).toHaveText(/^Taxi · about \d+ min · [\d.]+ km from Goa Grand$/);
  await expect(days.nth(1).locator("[data-travel]")).toHaveCount(1); // on day 2, before its stop
});
