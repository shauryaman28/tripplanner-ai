/**
 * Phase 40 — the settings page: the emails on or off, and when the reminder comes.
 *
 * Roadmap acceptance (Dev B):
 *   - the settings page renders and saves
 *   - notification_email false is respected        → the backend's tests (tests/unit/test_phase40_notifications.py)
 *   - the reminder timing is configurable           → here: saved, and read back after signing in again
 *
 * Runs on the Playwright stack's stub backend (tests/e2e/stub_backend.py): the real preferences route, on its
 * own database.
 */

import { expect, test, type Page } from "@playwright/test";

const EMAIL = `settings-${Date.now()}@example.com`;
const PASSWORD = "test-password-123";

test.describe.configure({ mode: "serial" }); // one account for the file

const saved = (page: Page) =>
  page.waitForResponse(
    (r) => r.url().endsWith("/api/users/preferences") && r.request().method() === "PUT" && r.status() === 200,
    { timeout: 15_000 },
  );

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/trips$/);
}

test("the settings page opens from the header with the emails on and the reminder a day before", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: /create one/i }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page).toHaveURL(/\/trips$/);

  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  const emails = page.getByRole("switch", { name: "Email notifications" });
  await expect(emails).toHaveAttribute("aria-checked", "true");
  await expect(page.getByLabel("Trip reminder")).toHaveValue("24");
  await expect(page.getByLabel("Trip reminder").locator("option:checked")).toHaveText("A day before");
  await expect(page.getByRole("form", { name: "Email notifications" }).getByText(EMAIL)).toBeVisible(); // where they go
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled(); // nothing changed yet
});

test("a reminder two days before is saved, and still chosen after signing out and in again", async ({ page }) => {
  await signIn(page);
  await page.goto("/settings");
  await page.getByLabel("Trip reminder").selectOption("48");

  const put = saved(page);
  await page.getByRole("button", { name: "Save" }).click();
  const answer = await (await put).json();
  expect([answer.notification_email, answer.reminder_hours_before]).toEqual([true, 48]);
  await expect(page.getByRole("form", { name: "Email notifications" }).getByRole("status")).toHaveText("Saved");

  await page.reload();
  await expect(page.getByLabel("Trip reminder")).toHaveValue("48"); // read back from the server

  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page).toHaveURL(/\/login$/);
  await signIn(page);
  await page.goto("/settings");
  await expect(page.getByLabel("Trip reminder")).toHaveValue("48");
});

test("emails turned off are saved off, the reminder greyed out — and a plan language chosen keeps them so", async ({ page }) => {
  await signIn(page);
  await page.goto("/settings");
  const emails = page.getByRole("switch", { name: "Email notifications" });
  await emails.click();
  await expect(emails).toHaveAttribute("aria-checked", "false");
  await expect(page.getByLabel("Trip reminder")).toBeDisabled();
  await expect(page.getByText("No emails are sent while notifications are off.")).toBeVisible();

  const put = saved(page);
  await page.getByRole("button", { name: "Save" }).click();
  const answer = await (await put).json();
  expect([answer.notification_email, answer.reminder_hours_before]).toEqual([false, 48]);

  // the header's language menu saves the whole of the preferences too: it must send these back as they are
  const language = saved(page);
  await page.getByLabel("Plan language").selectOption("hi");
  const after = await (await language).json();
  expect([after.preferred_language, after.notification_email, after.reminder_hours_before]).toEqual(["hi", false, 48]);

  await page.reload();
  await expect(page.getByRole("switch", { name: "Email notifications" })).toHaveAttribute("aria-checked", "false");
  await expect(page.getByLabel("Trip reminder")).toHaveValue("48");
});

test("a change is never undone by the page's own load answering late", async ({ page }) => {
  // Each read of the preferences is answered later than the one before. In development React loads the page twice
  // (and the header's language menu reads them too): an earlier load whose answer is kept would fill the form first,
  // and a later one, arriving after the traveller's change, would put the form back.
  await signIn(page);
  let asked = 0;
  let answered = 0;
  await page.route("**/api/users/preferences", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    asked += 1;
    await new Promise((later) => setTimeout(later, asked * 700));
    await route.continue();
    answered += 1;
  });
  await page.goto("/settings");
  const emails = page.getByRole("switch", { name: "Email notifications" });
  await expect(emails).toHaveAttribute("aria-checked", "false"); // as the test before left them
  await emails.click(); // as soon as the form is there
  await expect.poll(() => asked > 0 && answered === asked, { timeout: 15_000 }).toBe(true); // every load answered
  await expect(emails).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("button", { name: "Save" })).toBeEnabled();
});
