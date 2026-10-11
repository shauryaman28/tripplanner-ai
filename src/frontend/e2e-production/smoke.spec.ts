/**
 * A traveller plans a trip on the deployed site — in a browser, because a browser is what enforces the
 * two things a deployment gets wrong and a script does not notice:
 *
 *  - the page's event stream goes straight to the backend, which is another origin: unless the backend
 *    names this site in CORS_ORIGINS the browser refuses to let the page read it, and the progress panel
 *    says "Reconnecting…" for ever while everything else works;
 *  - everything else goes through this site's own /api/*, to wherever BACKEND_URL pointed when the site
 *    was BUILT.
 *
 * Run with playwright.production.config.ts (it says how). Real providers, real models: one plan.
 */

import { expect, test } from "@playwright/test";

const EMAIL = `browser-check-${Date.now()}@example.com`;
const PASSWORD = `check-${Math.random().toString(36).slice(2)}-A1`;

function daysFromNow(days: number): string {
  const day = new Date(Date.now() + days * 86_400_000);
  return day.toISOString().slice(0, 10);
}

test("a traveller plans a trip on the deployed site", async ({ page, baseURL }) => {
  const site = new URL(baseURL!).origin;
  const complaints: string[] = []; // what the browser's console says went wrong — a CORS refusal is said there
  const streamOrigins = new Set<string>();
  const apiOrigins = new Set<string>();
  page.on("console", (message) => {
    if (message.type() === "error") complaints.push(message.text());
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith("/stream")) streamOrigins.add(url.origin);
    else if (url.pathname.startsWith("/api/")) apiOrigins.add(url.origin);
  });

  // ── an account ───────────────────────────────────────────────────────────
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  // ── a trip, six weeks out (the flight provider does not sell seats a year ahead) ──
  await page.getByRole("button", { name: /plan a trip/i }).first().click();
  await page.getByLabel("Destination").fill("Jaipur");
  await page.getByLabel("Start date").fill(daysFromNow(45));
  await page.getByLabel("End date").fill(daysFromNow(48));
  await page.getByLabel(/budget/i).fill("70000");
  await page.getByLabel("Travellers", { exact: true }).fill("2");
  await page.getByLabel(/interests/i).fill("history, food");
  await page.getByRole("button", { name: /create & plan/i }).click();
  await expect(page).toHaveURL(/\/trips\/[a-f0-9-]{36}$/);

  // ── the event stream is open: the browser was allowed to read another origin's answer ──
  await expect(page.locator('[data-stream="connected"]')).toBeVisible();

  // ── the plan, as it arrives ───────────────────────────────────────────────
  const chat = page.getByRole("textbox", { name: /message the trip assistant/i });
  await chat.fill("A relaxed long weekend of forts and good food");
  await chat.press("Enter");
  const itinerary = page.getByLabel("Your itinerary");
  await expect(itinerary.getByRole("article").getByText("Day 1", { exact: true })).toBeVisible({ timeout: 180_000 });
  await expect(page.getByLabel("Trip cost").getByText("Estimated total")).toBeVisible();
  await expect(page.getByLabel("Planning progress").getByText(/planning complete/i)).toBeVisible();

  // ── where each kind of request went ───────────────────────────────────────
  expect(Array.from(apiOrigins)).toEqual([site]); // everything but the stream: this site's own /api/*
  expect(streamOrigins.size).toBe(1);
  const [backend] = Array.from(streamOrigins);
  if (!site.includes("localhost")) expect(backend.startsWith("https://")).toBe(true);
  expect(backend).not.toBe(site); // the stream: straight to the backend, past the frontend's host
  const refused = complaints.filter((line) => /CORS|Access-Control-Allow-Origin|blocked by/i.test(line));
  expect(refused).toEqual([]);

  // ── and it is still there for a page that was not watching ────────────────
  await page.reload();
  await expect(itinerary.getByRole("article").getByText("Day 1", { exact: true })).toBeVisible();
  console.log(`planned on ${site}; its event stream came from ${backend}; left behind: the account ${EMAIL}`);
});
