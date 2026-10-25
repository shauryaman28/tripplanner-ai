# Phase 40 — Notification System

STATUS_LINE
**What the phase delivers (roadmap):** Users receive email notifications at key moments.

Decisions: `DECISIONS.md` #359–#365. How to run it: `HOW_TO_RUN.md`, Step 38.

Three things are worth knowing before reading on.

- **Nothing is sent from this machine yet.** Without `SENDGRID_API_KEY` every email is written to the backend's
  log instead — whole in development, and in production only that it was not sent. Sending for real needs a
  SendGrid account, a key and a verified sender, all three yours to make (HOW_TO_RUN Step 38 D).
- **A trip begins when its flight leaves.** The roadmap's `start_date − 24h` has no hour in it: the reminder is
  counted from day 1's flight departure, or 08:00 on the first day when there is no flight — and says the day as
  seen from when it is sent, so a trip planned the night before its flight is reminded "today", not "tomorrow"
  (#361).
- **Each email is written down before it is sent.** One row per trip, kind and what it is about, unique in the
  database: however many backends look for reminders at once, one of them sends it (#362).

## What was built

```
src/backend/app/core/notifications.py (NEW)  ← the two emails composed; written down once; sent by SendGrid, or
                                               logged; when a trip begins; the reminder watcher
src/backend/app/models/notification.py (NEW), migrations/versions/009_notifications.py (NEW)
                                             ← notifications (unique by trip, kind, about); the two settings
src/backend/app/api/routes/trips.py          ← a trip's first itinerary: its email spawned as the run ends
src/backend/app/main.py                      ← the watcher, started and stopped with the app
src/backend/app/{models,schemas}/user_preferences.py, api/routes/users.py
                                             ← notification_email (on), reminder_hours_before (24; 1 to 168)
src/backend/app/core/config.py, preflight.py, .env.example
                                             ← SENDGRID_API_KEY, EMAIL_FROM, SITE_URL, REMINDER_CHECK_SECONDS; the
                                               pre-start check's three warnings
docker/prodlike/compose.yml, docker/loadtest/compose.yml
                                             ← the stack takes the key from .env; the load test never has one
src/frontend: app/settings/page.tsx (NEW); AppHeader.tsx, lib/api.ts, lib/types.ts
                                             ← the settings page, the gear that opens it, saving the two
scripts/notify_check.py (NEW)                ← two real plans and their emails, on a running stack
tests/unit/test_phase40_notifications.py, tests/integration/test_phase40_notifications_integration.py,
e2e/settings.spec.ts (NEW); tests/conftest.py, tests/e2e/stub_backend.py ← no test sends an email
docs/phase40_live/                           ← the live check
```

## Dev A — email notifications

- **SendGrid, or the log** — one POST to `/v3/mail/send` with httpx; 202 is sent, anything else failed with why;
  no key, the log (#364).
- **"Your itinerary is ready"** — on a trip's first itinerary, spawned as the planning run ends: its days, places,
  total and link (#359, #360).
- **"Your trip starts tomorrow"** — `reminder_hours_before` before the trip begins, by a watcher in each backend
  every five minutes; day 1's flight, weather, stops and hotel (#361, #363).
- **Once** — written down before it is sent, unique by trip, kind and what it is about (#362).

## Dev B — notification preferences

- **`notification_email`** (on) and **`reminder_hours_before`** (24; 1 to 168), migration 009; off is written
  down "off" and nothing is sent (#365).
- **The settings page** — the switch, the reminder's timing, Save; opened by the gear in the header (#365).

## Found and fixed along the way

- **A reminder already sent made every trip after it fail.** One session looked at every trip; a reminder claimed
  before was rolled back, and the rollback expired the other trips the session held — the next one failed on
  reading its own id. Each trip is now reminded in a session of its own (#363). Found by the integration test of a
  traveller with two trips.
- **"Starts tomorrow" for a trip that starts today.** The reminder said "tomorrow" whenever the setting was 24
  hours; one sent late — a trip planned the night before — would have been wrong. It is said from when it is sent
  (#361).
- **The settings page could undo a change.** In development React loads a page twice; the second answer, arriving
  after the traveller's change, put the form back. Only the latest load fills it now (#365). Found by the browser
  test.
- **The load test would have emailed.** The load-test stack replaces every key the production-like stack takes
  from `.env` — a Phase 30 test holds it to that, and failed when the new key was added: a load test's plans would
  have been real emails with a key in `.env`. It has none now (#364).

## The order things were done in

ORDER

## Live results

LIVE_RESULTS

## Tests

| | New | Total |
|---|---|---|
| Unit + contract | 46 (`test_phase40_notifications.py`) | 1,768 |
| Integration | 5 (`test_phase40_notifications_integration.py`) | 113 |
| Browser | 4 (`e2e/settings.spec.ts`) | 79 |

## Break it on purpose

On a scratch copy of the repository (its own database `tripplanner_m40`, Redis number 15, and its own stub backend
and Next server on ports 8140 and 3140 for the site), each breakage alone, the tests run after it.

**34 of 36 caught in the first round**, the other two once their tests were made stronger: **36 of 36.**

- **The mailer:** sent with no key; the whole email logged in production; any 2xx counted as sent; no sender
  check; the key not sent as a Bearer token; no html part.
- **What the emails say:** free time counted as a place; the plan's words not escaped in the html; no link to the
  trip; the reminder's day taken from the setting; no flight line.
- **When:**
  - a time with no offset read by the server's clock;
  - a flight of another day believed;
  - a trip with no flight beginning at midnight;
  - the traveller's hours ignored;
  - still due after the trip has begun;
  - reminders looked for one day ahead, not eight;
  - a trip that begins today not looked at.
- **Once, and off:** the claim not unique; "off" ignored; no preferences counted as off; the ready email raising
  into its run.
- **The run and the watcher:** the run waiting for the email; every itinerary emailed; 0 not turning the watcher
  off.
- **The settings:** any hour accepted; neither setting saved by the route; no pre-start warning; the load-test
  stack given the key.
- **On the site, in the browser:** only the reminder saved; the language menu resetting the emails; the reminder
  not greyed out when emails are off; no way to the settings from the header.

**Not caught at first, and why:**

- **"a failed look ends the watcher" timed out.** The test waited, with no deadline, for a third look that a
  stopped watcher never makes. A timeout is not a catch. The wait now has a two-second deadline, and the test
  fails.
- **"a late load undoes the change" survived as flaky.** The browser test failed once and passed on its retry:
  the late answer only sometimes came after the change. A new test answers each load later than the one before,
  so it fails every time.

## Done criterion checklist

- [x] Email logged (dev) or sent (prod) on planning_complete — the integration test through the app; live, logged
- [ ] …sent — waits for your SendGrid key and verified sender (HOW_TO_RUN Step 38 D)
- [x] Reminder fires at the correct time (tested with an injectable now()) — `due_reminders(now)`, the integration tests; live, by the watcher
- [x] No HTTP delay from email sending — the unit test: the run ends while its email waits; the request was answered before the run began
- [x] notification_email false is respected — unit and integration tests; live
- [x] Reminder timing is configurable and respected — 48 hours, in the integration tests and live
- [x] Settings page renders and saves — the browser test

## Known limits

- **Sent only with your SendGrid account.** The production-like stack has none: its emails are written down
  "logged", with a line in its log. Free SendGrid sending is around 100 emails a day; whether that is a lasting
  free tier or a 60-day trial is reported differently by different sources — check SendGrid's own pricing page.
- **A failed email is not tried again.** The row says why (#362).
- **At most five minutes late.** The watcher looks every `REMINDER_CHECK_SECONDS` (#363).
- **A backend stopped mid-send** leaves its row "sending", and that email is not sent by another.
- **India's time.** A departure with no offset is read as India's (#361); the planner plans trips within India.
- **Email only.** No push or SMS; the roadmap asks for email.
