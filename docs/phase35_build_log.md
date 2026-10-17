# Phase 35 — Security Hardening

**Status: built, and checked on the production-like stack in production mode — headers, signing in, staying signed in, the limit, the inputs — and on a real plan through the site's production build. The key rotation the roadmap asks for waits for the user (the keys are theirs): everything for it is ready and was run on the keys as they are (HOW_TO_RUN Step 33 C). NOT on a deployment: there is none yet.** What that leaves open is in [Known limits](#known-limits).
**What the phase delivers (roadmap):** OWASP Top-10 relevant items reviewed and addressed. API keys never logged.

Decisions: `DECISIONS.md` #315–#330 (#326 is the OWASP Top 10, item by item). How to run it: `HOW_TO_RUN.md`, Step 33.

Four things are worth knowing before reading on.

- **The limit on guessing passwords would have been a header away.** The image trusted every caller about `X-Forwarded-For` (`--forwarded-allow-ips '*'`), and uvicorn then takes the leftmost entry — the one the client writes. A per-address limit built on that address limits nobody who sends a new made-up address each time. The image now believes the header from loopback and private networks only, and reads it from the right (#320). On the stack: a made-up address for each attempt, still 429.
- **Three more things nobody had asked about.** bcrypt reads 72 bytes of a password and ignores the rest, and the server checked no minimum at all — only the page said 8 (#322). The JSON log lines were scrubbed and the text ones were not, so a development console wrote what it was given (#328). This machine's Google key is in the newer `AQ.…` form, which the roadmap's `AIza…` pattern would not have caught (#328).
- **Eight vulnerable Python packages, now none; the site's Next.js is a major version behind its fixes.** `python-jose`, the JWT library, has an advisory with no fix: the app moved to PyJWT and three packages are gone (#323). Next 14 has advisories that only Next 15.5 fixes; the reachable critical one is closed by switching off an image optimizer the site never used, and the rest are read one by one (#324). The upgrade is the first thing to do next.
- **No key in 171,000 lines of logs, in the repository, or in any of its 91 commits** — searched for by every key's value, not only by shape (#329).

## What was built

```
src/backend/app/core/headers.py          ← NEW: the security headers on every answer — a 500 too; the docs' own policy
src/backend/app/core/ratelimit.py        ← NEW: five attempts in any sixty seconds per address — a sliding window in Redis, one Lua script
src/backend/app/core/sessions.py         ← NEW: refresh tokens — issued, used once, rotated in a family; used twice ends the sign-in
src/backend/app/models/refresh_token.py  ← NEW, migrations/versions/007_refresh_tokens.py: the hashes, never the tokens
src/backend/app/schemas/text.py          ← NEW: a place, a traveller's own words, a password — what each may hold, and how much
src/backend/app/api/routes/auth.py       ← login with the limit and a bounded form; register with its limit; POST /auth/refresh, /auth/logout
src/backend/app/schemas/{trip,auth,user_preferences}.py, routes/{trips,admin}.py, api/deps.py
                                         ← every string and list the API takes has a length (a test reads the API's schema)
src/backend/app/core/security.py, deps.py← PyJWT in place of python-jose
src/backend/app/core/config.py           ← JWT_EXPIRE_MINUTES 60; REFRESH_*; LOGIN_ATTEMPTS_PER_MINUTE, REGISTRATIONS_PER_MINUTE
src/backend/app/core/error_reporting.py  ← the scrubber knows what providers' keys look like
src/backend/app/preflight.py             ← a deployment told when it switches a protection off
src/backend/app/main.py                  ← the headers' layer; the 500's headers; every log record redacted
src/telemetry/logs.py                    ← LogRedactor, on every record as it is made — text and JSON, both processes
src/ai/mcp_server/server.py              ← the tool server redacts its records too
src/ai/orchestrator/orchestrator.py, agents/refinement_classifier.py  ← the traveller's words called untrusted in two more prompts
src/backend/Dockerfile, docker/loadtest/Dockerfile  ← FORWARDED_ALLOW_IPS: loopback and private networks, not everybody
docker/prodlike/compose.yml              ← passes FORWARDED_ALLOW_IPS through; docker/loadtest/compose.yml: the limits off there
src/frontend/src/lib/api.ts, sse.ts, components/AppHeader.tsx  ← refresh on 401 (once, shared); a fresh token for each stream;
                                           sign-out ends the sign-in on the server
src/frontend/next.config.mjs             ← the pages' own headers; the image optimizer off
requirements.txt, constraints.txt, src/frontend/package.json  ← PyJWT, the upgrades; Next 14.2.35
scripts/security_check.py                ← NEW: Phase 35's protections, as a stranger finds them — 21 checks
scripts/secret_audit.py                  ← NEW: a key anywhere it should not be — logs, Loki, containers, the repository, its history
scripts/provider_keys.py                 ← NEW: does each key in .env open its provider — one question each, no quota
.env.example, .env                       ← JWT_EXPIRE_MINUTES 60 (it was 1440 in both); the new settings
tests/unit/test_phase35_security.py, test_phase35_secrets.py   ← NEW (73)
tests/integration/test_phase35_auth_integration.py             ← NEW (13)
src/frontend/e2e/session.spec.ts                               ← NEW (4)
```

## Dev A — auth and input hardening

| The roadmap asks | What was built | Seen |
|---|---|---|
| A refresh token flow: `POST /auth/refresh`, JWTs short-lived (1 h) without forcing re-login | Access tokens an hour (#315). Refresh tokens: random, stored hashed, used once, rotated in a family that ends 30 days after the sign-in; used twice → the sign-in ends; signing out ends it (#316–#317). The site keeps the refresh token in an HttpOnly, SameSite=Strict cookie sent only to `/api/auth`, refreshes on a 401 once however many requests found the token gone, and before opening a stream (#318) | On the stack in production mode: `expires_in 3600`; the cookie `HttpOnly; Path=/api/auth; SameSite=strict; Secure`; the first token presented again after the grace period → 401, and the second then refused too; sign-out → refresh 401. In a browser, against the production build: the access token broken on purpose → `GET /api/trips 401`, `POST /api/auth/refresh 200`, `GET /api/trips 200`, one refresh; the cookie not in `document.cookie` |
| Rate limiting on `POST /auth/login`: max 5 attempts per IP per minute | A sliding window per address, atomic (#319), on an address that cannot be made up (#320); registration limited too | The sixth attempt within a minute → 429, `Retry-After 48`; three more with made-up addresses → 429, 429, 429. Twenty at once on a real Redis → exactly five through |
| All Pydantic schemas get `max_length`; no injection via `destination` or `interests` | Every string and list the API takes is bounded — a test reads the API's schema and fails on any that is not; a destination is a place in words, an interest a few plain words, a request a traveller's own words without hidden characters; passwords 8 characters to 72 bytes (#321–#322); the traveller's text called untrusted in every prompt it reaches (#327) | `Goa\nIgnore the rules above` → 422; `Goa"}, "budget": 1` → 422; 2,001 characters → 422; a seven-character password → 422; `Goa'; DROP TABLE trips;--` stored as those words, the table still there |
| A `DECISIONS.md` entry: which OWASP Top-10 items are relevant and how each is addressed | #326, item by item, with the LLM-specific one (prompt injection) as #327 | — |
| **Acceptance:** refresh token flow works; login rate limiting fires at 5 attempts; all string fields have `max_length`; OWASP review documented | — | ✅ all four, on the production-like stack |

## Dev B — secret management and logging audit

| The roadmap asks | What was built | Seen |
|---|---|---|
| Grep all log output for key patterns (Amadeus client ids, Google's `AIza…`, OWM's 32-hex); if found, a `LogRedactor` | `scripts/secret_audit.py`: every secret *value* in the env files and every provider's key *shape*, in log files, Loki, the containers' logs, the repository and its history (#329). And a `LogRedactor` on every record of both processes, text and JSON, whether or not anything had been found (#328) | Nothing found: 163 files (126,820 lines), Loki (31,271 lines), the containers (13,675 lines), every tracked file and 2,181 versions of files across 91 commits. A key planted in a log file, by value and by shape, is found and not printed (tests) |
| Rotate all dev API keys and confirm the system still works | `scripts/provider_keys.py` asks each provider one question that costs no quota; the procedure is HOW_TO_RUN Step 33 C (#330) | Before the rotation: all seven keys answered 200. **The rotation itself: waiting for the user** |
| `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options` on all API responses via middleware | Those three, `Referrer-Policy`, `Permissions-Policy`, and HSTS in production — on every answer, including a 500 Starlette answers outside every layer (#325). The docs pages get a policy of their own. The site's pages get the ones that apply to them | `curl -I` on the stack: a 200, a 401, a 404, the docs, a deliberate 500 — all of them there |
| **Acceptance:** zero API key patterns in log output; key rotation test passes; security headers present on all responses (`curl -I`) | — | ✅ zero; ✅ headers; ◻ the rotation — the user's |

## Found and fixed along the way

**The forwarded address** (#320). Found reading how uvicorn resolves a client before writing the limit: with `--forwarded-allow-ips '*'`, `X-Forwarded-For`'s *first* entry — the one the client writes. Fixed in the image, and in the load test's. A unit test runs uvicorn's own middleware on the image's setting; the stack, made to treat this machine as a stranger (`FORWARDED_ALLOW_IPS=127.0.0.1`), refused every made-up address. With the image's default it believes them — this machine reaches the stack through Docker's private gateway, which is where a platform's own proxy would connect from — which is the setting working, and why a deployment's proxy must be the only one it trusts.

**bcrypt's 72 bytes, and a minimum only the page knew** (#322). Measured on this machine: `"a"×73` verified against the hash of `"a"×72`; a 100-character password against its first 72. And `POST /auth/register` took `pass123`.

**Text log lines were not scrubbed** (#328). The scrubber ran inside the JSON formatter. The redactor runs on the record as it is made, so a library's own handler and a development console get the same clean line — a test logs through a handler of its own with no filter on it, and the key is gone.

**The newer Google key** (#328). This machine's `GOOGLE_API_KEY` starts `AQ.`, not `AIza`: a redactor or an audit built on the roadmap's pattern would have missed the one Google key in use.

**`python-jose` and `ecdsa`: advisories with no fix** (#323). Not reachable in this app — but rather than keep a JWT library with an open advisory, the app moved to PyJWT, already installed; `python-jose`, `ecdsa` and `rsa` are gone. Five more packages upgraded within their lines; `pip-audit` afterwards: no known vulnerabilities.

**Next's image optimizer, on in a site that never used it** (#324). `/_next/image` carried two of the critical advisories; switched off, it answers 404.

**Two prompts that did not call the traveller's words untrusted** (#327). The intent parser's and the refinement classifier's; the others already did.

**What my own tests got wrong first.** A test of an expired refresh token passed `datetime.now()` (local time, India) to a column the app keeps in UTC — "a second ago" was five and a half hours ahead, and the token was accepted; the test was wrong, not the app. The deep-check tests of Phase 34 had taught the same lesson the other way: a test that reads `settings` must set what it reads — here, the suites' switch for the limits first went into the environment, where every `Settings(...)` a test made of a deployment saw it too; it is a fixture on the live settings now. And a browser test asserted before the page had asked again (the button it waited for is drawn before the list arrives); it waits for the request now.

## The order things were done in

Times are the first moment each appears in the session's record, or `date` in the command that ran it.

| When (IST, 17 Oct) | What |
|---|---|
| 08:00 | Begun: main at the Phase 34 merge (`f295960`); battery 36 %, on battery |
| 08:03 | `--forwarded-allow-ips '*'` read, and what uvicorn does with it |
| 08:05–08:18 | The headers, the text rules, the limit, the refresh tokens, migration 007; the suites (1,447 unit, 83 integration); the browser tests of signing in |
| 08:19–08:30 | The audit: logs, Loki, containers — nothing found (08:21–08:30); the repository and its history — nothing found (08:30) |
| 08:31 | The LogRedactor, and the scrubber's key shapes |
| 08:33 | The provider keys, before any rotation: seven of seven answered |
| 08:33–08:39 | `pip-audit`: eight packages → none; `npm audit`; Next 14.2.35; the image optimizer off |
| 08:46–08:53 | The stack rebuilt (0.35.0): `curl -I`; `security_check.py` 21 of 21 (08:50); Phase 27's check 29 of 29 with a real plan (08:52); the production browser test; the session browser tests on the production build |
| 08:55 | The break-on-purpose checks |
| 08:58–09:01 | The breakages again, after three tests were made to hold what they claimed: **45 of 45** |
| 09:01–09:03 | Two breakages of the site's code, on a copy, under the browser tests: 2 of 2 |

## Live results

### The production-like stack, production mode (17 October)

`python scripts/security_check.py --prodlike` with `FORWARDED_ALLOW_IPS=127.0.0.1`: **21 passed, 0 failed** (the listing is in HOW_TO_RUN Step 33 A). `curl -sI` of a 404:

```
HTTP/1.1 404 Not Found
cache-control: no-store
x-content-type-options: nosniff
x-frame-options: DENY
content-security-policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
referrer-policy: no-referrer
permissions-policy: camera=(), microphone=(), geolocation=(), payment=()
strict-transport-security: max-age=31536000; includeSubDomains
```

`python scripts/prod_check.py --prodlike --web http://localhost:3200` — the image as deployed, the site built for production in front of it, real providers and models: **29 passed, 0 failed, 0 skipped**; the schema at migration 007; a real plan over the stream in 11.6 s (with the intent prompt's new sentence, on the real Gemini); the site's `/api` reaching the backend. Then `npx playwright test -c playwright.production.config.ts` — a traveller plans a trip on the site in a browser: passed. And the four session tests run against that production build instead of the stub stack: passed — the `Secure` cookie through Next's proxy, the refresh, the sign-out, the headers.

### The audits

```
python scripts/secret_audit.py logs
10 secret values from .env, .env.prodlike; 12 shapes
163 files, 126,820 lines
Loki: 31,271 lines
the running containers' logs: 13,675 lines
nothing found

python scripts/secret_audit.py repo
tracked files, and 2181 versions of files across 91 commits
nothing found

pip-audit -r constraints.txt   (before)  8 packages with known vulnerabilities  →  (after)  No known vulnerabilities found
npm audit --omit=dev           (before)  next 14.2.5 critical, postcss high  →  (after)  next 14.2.35: the Next 14 advisories (#324)
```

### The keys

```
python scripts/provider_keys.py        (08:33, before the rotation)
  ✓ GROQ_API_KEY           Groq         200 in 344 ms
  ✓ GOOGLE_API_KEY         Gemini       200 in 569 ms
  ✓ DUFFEL_ACCESS_TOKEN    Duffel       200 in 2656 ms
  ✓ LITEAPI_API_KEY        LiteAPI      200 in 923 ms
  ✓ OPENTRIPMAP_API_KEY    OpenTripMap  200 in 1219 ms
  ✓ OPENWEATHER_API_KEY    OpenWeather  200 in 208 ms
  ✓ LANGCHAIN_API_KEY      LangSmith    200 in 469 ms
  – ANTHROPIC_API_KEY      Anthropic    not set
```

The rotation: waiting for the user — new keys in `.env`, this again, the old keys deleted, this once more, and a plan (HOW_TO_RUN Step 33 C).

## Tests

**1,528 unit + contract, 96 integration, 64 browser — all passing.** New in this phase: 73 unit, 13 integration, 4 browser.

- **`tests/unit/test_phase35_security.py` (56)** — the headers on seven kinds of answer, a 500, the docs (every address the page loads allowed by its policy), HSTS only in production, a stream not wrapped, an answer's own header kept, the site's pages; every field of every request bounded, read from the API's schema — and the walk shown to find a field added without one; destinations in any script taken, and every way of carrying something else refused; interests on the trip as on a traveller; a traveller's own words; passwords at both ends; the login form; the limiter's verdicts, its silence when off, Redis away (let through, logged, the address hashed), a 429 that costs no bcrypt, registration; whom uvicorn believes, on the image's own setting; the pre-start check's warnings; the prompts.
- **`tests/unit/test_phase35_secrets.py` (17)** — the LogRedactor on a text line (a key by value, by shape, in a query string, a login token, an e-mail — and a trace id and a cache digest left alone), on a traceback, on a library's own handler; installed once, in both processes; the scrubber taking out whatever the audit finds; the audit's secrets (published defaults excepted), a planted key found and not printed, the exit codes, a key committed once and taken out again found in the history.
- **`tests/integration/test_phase35_auth_integration.py` (13)** — real Postgres and Redis: a sign-in's tokens and cookie, Secure in production; a refresh by body and by cookie; two tabs at once; a stolen token; signing out (another sign-in untouched); expired and unknown tokens; the thirty days; what a new sign-in clears away; the sixth attempt, another address unaffected; the window sliding and a refused attempt not counted; twenty at once; a destination that looks like SQL.
- **`src/frontend/e2e/session.spec.ts` (4)** — the cookie no script can read, a broken access token refreshed unseen (one refresh); signing out ends it on the server; a broken token after sign-out → sign in again; the pages' headers.
- Changed: `test_auth.py` (an eight-character password), `test_phase29_wiring.py` (one more layer), `tests/conftest.py` (the limits off for the suites, on the live settings).

## Break it on purpose

47 deliberate breakages. 45 applied to a scratch copy of the repository with a database and a Redis number of its own, the Phase 35 tests (and those of the earlier phases that read the same files) run on it, and the integration tests when a breakage is to the database or Redis; 2 to the site's code, on a copy with its own build directory, under the browser tests. **First pass, 42 of 45; after three tests were made to hold what they said: 45 of 45 — 31 caught by the unit tests, 14 by the integration tests — and 2 of 2 in the browser.**

The three that survived the first pass were tests of mine that checked less than they said:

| The breakage | Why nothing failed | Now |
|---|---|---|
| the docs' policy without the CDN in `script-src` | the test asked whether the CDN's address was *anywhere* in the policy — it still was, in `style-src` | each script checked against `script-src`, each stylesheet against `style-src`, each icon against `img-src`; and it fails if it finds no script at all |
| the API's policy loosened to `default-src 'self'` | the test compared the answer with the module's own constant, which the breakage changed too | the policy written out in the test |
| a window that never slides | the test waited out the whole window, and the key's expiry emptied it anyway | attempts staggered within the window: a window that never slides refuses all three of the last, a key expiring whole lets all three in; only a sliding one lets two through |

| Where | Breakages | For example |
|---|---|---|
| the headers | 6 | the layer not added · a 500 without them · HSTS on http · an answer's own overwritten · the API's policy loosened · the docs' |
| what the API takes | 9 | a destination of any text · a line break in a place · format characters in free text · a request of any length · the trip's interests unchecked · six-character passwords · past 72 bytes · an email of any length · a stream's token of any length |
| the limit | 8 | sign-ins not counted · accounts not counted · a refused attempt counted · a window that never slides · no `Retry-After` · Redis away locking everyone out · the address in the log · the image believing everybody |
| the refresh tokens | 13 | never used up · used twice never ending the sign-in · two tabs ending it · revoked or expired still good · the token stored, not its hash · no end to a sign-in · signing out ending nothing · the cookie readable, sent everywhere, cross-site, never Secure · a day's access token |
| the logs and the audit | 7 | the redactor not installed · a traceback left as it was · Google's new keys unknown · the tool server not redacting · the history not read · a finding printed whole · published defaults taken for secrets |
| the prompt and the pre-start check | 2 | the request not called untrusted · the limit off unsaid |
| the site (browser) | 2 | no refresh on a 401 · sign-out only in this browser |

## Done criterion checklist

| | Criterion | |
|---|---|---|
| ✅ | Refresh token flow works | API, integration, browser; on the stack in production mode |
| ✅ | Login rate limiting fires at 5 attempts | the sixth → 429 with `Retry-After`; made-up addresses not believed |
| ✅ | All string fields have `max_length` | a test reads every request field of the API's schema |
| ✅ | OWASP review documented | DECISIONS #326, #327 |
| ✅ | Zero API key patterns in log output | 171,766 lines of logs; the repository and its history |
| ◻ | Key rotation test passes | ready, and run on the keys as they are; the rotation is the user's (HOW_TO_RUN Step 33 C) |
| ✅ | Security headers present on all responses (`curl -I`) | every kind of answer, a 500 included |
| ◻ | …on a deployment | there is none |

## Known limits

- **Next.js 14.** Twenty-three advisories that only Next 15.5 or 16 fix; the reachable critical one is closed (the image optimizer, off), the rest read one by one (#324). What remains reachable: denial of service through Server Components, cache confusion of responses to requests with bodies, and request smuggling in rewrites where Next does them itself. **The upgrade to Next 15.5 or later is the first thing to do next** — a major one (React 19).
- **The access token is still in `localStorage`** — an hour of it is readable by a script that gets onto the page. Out of reach of scripts entirely means the site's server holding the session; and the site's pages restrict frames and objects but not which scripts run (a policy for those needs a nonce per request) (#318, #325).
- **The limit is per address, and on a deployment the address may be the proxy's.** Every sign-in made through the site reaches the backend from Vercel's servers, so the limit there counts per Vercel address; and if Railway's proxy connects from a public address, every request would seem to come from it. Neither can be seen without a deployment; HOW_TO_RUN Step 33 E says how to tell and what to set (#320).
- **No second factor, no lockout per account.** A guesser spread over many addresses is limited only by bcrypt's pace. Refused sign-ins and a refresh token used twice are logged but not alerted on.
- **A refresh token issued before a sign-out** stays good until the family is revoked — which a sign-out does at once; an access token already handed out stays good for what is left of its hour (it is checked by its signature, not looked up).
- **Build tools with advisories** (ESLint's, Tailwind's, PostCSS's) — run on a developer's machine and in CI on this repository's own files; fixed only by major versions (#324).
