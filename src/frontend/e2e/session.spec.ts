/**
 * Phase 35 — staying signed in, and signing out, in the browser.
 *
 * Roadmap: "a refresh token flow so JWTs can be short-lived (1 h) without forcing re-login".
 *
 * The access token is in localStorage; the refresh token is an HttpOnly cookie the page cannot read,
 * sent only to /api/auth/*. These tests break the access token on purpose — the way an hour does — and
 * watch the page carry on: the API's 401, one refresh through the cookie, the request again. Signing
 * out ends the sign-in on the server, so the cookie that is left behind refreshes nothing.
 *
 * Runs on the Playwright stack's stub backend (tests/e2e/stub_backend.py) — the real app, the real
 * auth routes and database; the providers stubbed.
 */

import { expect, test, type Page } from "@playwright/test";

const EMAIL = `session-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";

test.describe.configure({ mode: "serial" }); // one account for the file

async function register(page: Page) {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

/** What an hour does to a token: it stops being accepted. Here, a token signed by nobody. */
async function breakTheAccessToken(page: Page) {
  await page.evaluate(() => localStorage.setItem("tp_token", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4IiwiZXhwIjoxfQ.no"));
}

test("the refresh token is a cookie the page cannot read, and an access token that stops working is refreshed unseen", async ({ page, context }) => {
  await register(page);

  // the cookie: HttpOnly, sent only to the sign-in routes, strict about where a request comes from
  const cookie = (await context.cookies()).find((c) => c.name === "tp_refresh");
  expect(cookie).toBeDefined();
  expect(cookie!.httpOnly).toBe(true);
  expect(cookie!.path).toBe("/api/auth");
  expect(cookie!.sameSite).toBe("Strict");
  expect(await page.evaluate(() => document.cookie)).not.toContain("tp_refresh");
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(cookie!.value);

  // the access token stops working; the traveller opens their trips and notices nothing
  await breakTheAccessToken(page);
  const asked: string[] = [];
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/")) asked.push(`${response.request().method()} ${path} ${response.status()}`);
  });
  const retried = page.waitForResponse((r) => r.url().endsWith("/api/trips") && r.status() === 200);
  await page.reload();
  await retried; // the page's list came, the second time it was asked
  await expect(page).toHaveURL(/\/trips$/);
  expect(asked).toContain("GET /api/trips 401");
  expect(asked).toContain("POST /api/auth/refresh 200");
  expect(asked.indexOf("POST /api/auth/refresh 200")).toBeLessThan(asked.lastIndexOf("GET /api/trips 200"));
  expect(asked.filter((line) => line.startsWith("POST /api/auth/refresh")).length).toBe(1); // one refresh, not one a request

  // a new access token is in place, and the cookie was exchanged for the next one
  expect(await page.evaluate(() => localStorage.getItem("tp_token"))).not.toContain(".no");
  const next = (await context.cookies()).find((c) => c.name === "tp_refresh");
  expect(next!.value).not.toBe(cookie!.value);
});

test("signing out ends the sign-in on the server: the cookie left behind refreshes nothing", async ({ page, context }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
  const before = (await context.cookies()).find((c) => c.name === "tp_refresh")!;

  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect((await context.cookies()).find((c) => c.name === "tp_refresh")).toBeUndefined();

  // even with the old cookie put back by hand, the server has ended that sign-in
  await context.addCookies([{ ...before }]);
  const refreshed = await page.evaluate(async () => (await fetch("/api/auth/refresh", { method: "POST" })).status);
  expect(refreshed).toBe(401);
});

test("an access token that stops working after sign-out sends the traveller to sign in again", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
  await page.evaluate(async () => {
    await fetch("/api/auth/logout", { method: "POST" }); // ended elsewhere: the cookie is gone
  });
  await breakTheAccessToken(page);
  await page.reload();
  await expect(page).toHaveURL(/\/login$/); // nothing left to refresh with
});

test("the site's pages say they may not be framed or sniffed", async ({ page }) => {
  const answer = await page.goto("/login");
  const headers = answer!.headers();
  expect(headers["x-frame-options"]).toBe("DENY");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
  // and the API behind the site's proxy answers with its own
  const api = await page.request.get("/api/version");
  expect(api.headers()["content-security-policy"]).toContain("default-src 'none'");
  expect(api.headers()["x-frame-options"]).toBe("DENY");
});
