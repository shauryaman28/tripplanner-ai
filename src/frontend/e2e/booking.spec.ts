/**
 * Phase 39 — "Book flight" and "Book hotel".
 *
 * Roadmap acceptance (Dev B): the buttons are there and work; they open a new tab; they are greyed out when there
 * is no link. On the Playwright stack's stub backend (tests/e2e/stub_backend.py), whose flight and hotel searches
 * put on each result the link the real tools make (src/ai/booking.py) — except for Tirthan, whose have none. The
 * test answers Google's and Booking.com's addresses itself: it never reaches either.
 */

import { expect, test, type Page } from "@playwright/test";

import { bookingUrl } from "../src/lib/booking";

test("a link opens only if it is one of the two searches the planner makes", () => {
  expect(bookingUrl("https://www.google.com/travel/flights?q=Flights%20to%20GOI")).not.toBeNull();
  expect(bookingUrl("https://www.booking.com/searchresults.html?ss=Goa")).not.toBeNull();
  for (const other of ["javascript:alert(1)", "http://www.booking.com/searchresults.html?ss=Goa", "https://evil.example/?q=1", "", null, undefined]) {
    expect(bookingUrl(other)).toBeNull();
  }
});

const YEAR = new Date().getFullYear() + 1;

async function planTrip(page: Page, destination: string, start: string, end: string) {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(`booking-${Date.now()}@example.com`);
  await page.getByLabel("Password", { exact: true }).fill("test-password-123");
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill(destination);
  await page.getByLabel("Start date").fill(start);
  await page.getByLabel("End date").fill(end);
  await page.getByLabel(/budget/i).fill("60000");
  await page.getByLabel(/interests/i).fill("history");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("A long weekend");
  await chat.press("Enter");
  const days = page.getByLabel("Your itinerary").getByRole("article");
  await expect(days.getByText("Day 1", { exact: true })).toBeVisible({ timeout: 90_000 });
  return days;
}

test("Book flight and Book hotel open the filled-in search in a new tab", async ({ page, context }) => {
  // the two booking sites, answered here
  await context.route(/^https:\/\/www\.(google|booking)\.com\//, (route) =>
    route.fulfill({ contentType: "text/html", body: "<title>booking site</title>" }),
  );
  const days = await planTrip(page, "Goa", `${YEAR}-08-03`, `${YEAR}-08-05`);

  const flight = days.nth(0).locator('[data-book="Book flight"]');
  await expect(flight).toHaveAttribute("target", "_blank");
  await expect(flight).toHaveAttribute("rel", "noopener noreferrer");
  const flightUrl = new URL((await flight.getAttribute("href"))!);
  expect(flightUrl.origin + flightUrl.pathname).toBe("https://www.google.com/travel/flights");
  // the trip's form says two travellers unless told otherwise: the search asks for two
  expect(flightUrl.searchParams.get("q")).toBe(`Flights to GOI from DEL on ${YEAR}-08-03 through ${YEAR}-08-05 for 2 adults`);

  const hotel = days.nth(0).locator('[data-book="Book hotel"]');
  const hotelUrl = new URL((await hotel.getAttribute("href"))!);
  expect(hotelUrl.searchParams.get("ss")).toBe("Goa Grand, Goa");
  expect([hotelUrl.searchParams.get("checkin"), hotelUrl.searchParams.get("checkout")]).toEqual([`${YEAR}-08-03`, `${YEAR}-08-05`]);
  expect([hotelUrl.searchParams.get("group_adults"), hotelUrl.searchParams.get("no_rooms")]).toEqual(["2", "1"]);
  await expect(days.nth(1).locator('[data-book="Book hotel"]')).toHaveCount(0); // the stay's first night only

  // a click opens a new tab, at the link — and this page stays where it is
  const [tab] = await Promise.all([context.waitForEvent("page"), flight.click()]);
  await expect(tab).toHaveURL(flightUrl.href);
  expect(page.url()).toMatch(/\/trips\/[a-f0-9-]{36}$/);
  const [second] = await Promise.all([context.waitForEvent("page"), hotel.click()]);
  await expect(second).toHaveURL(hotelUrl.href);
});

test("with no link, the buttons are there, greyed out, and open nothing", async ({ page }) => {
  const days = await planTrip(page, "Tirthan", `${YEAR}-08-13`, `${YEAR}-08-15`);
  for (const label of ["Book flight", "Book hotel"]) {
    const button = days.nth(0).locator(`[data-book="${label}"]`);
    await expect(button).toBeVisible();
    await expect(button).toHaveAttribute("aria-disabled", "true");
    expect(await button.getAttribute("href")).toBeNull();
  }
});
