# Phase 27 — Production Infrastructure

**Status: built, and verified end to end on a production-like stack on one machine. NOT yet deployed to Railway, Vercel, Neon, Upstash or Sentry: this build had no account on any of them.** What that leaves open, and the steps that close it, are in [The deploy itself](#the-deploy-itself).
**What the phase delivers (roadmap):** Full pipeline runs against production infrastructure end-to-end. Sentry catches a test error.

Decisions: `DECISIONS.md` #191–#207.

Four things are worth knowing before reading on.

- **Nothing was deployed.** This machine had no account, CLI or credential for any of the five services. So, as
  in Phase 26, the build reproduces what makes each service different from a laptop — TLS-only, a pooler in
  transaction mode, a password, closed connections, a stop signal, settings from the environment alone — and is
  exact about what that does not prove (#207). The steps of the real deploy are in `HOW_TO_RUN.md`, Step 25 B.
- **The roadmap's "document every missing env var found on first deploy" was done by deploying the old code
  first** — onto that stack. What it did is under [The first deploy of the code as it was](#the-first-deploy-of-the-code-as-it-was).
- **The worst finding was not a variable.** The image had installed newer libraries than any test had run on, and
  the first request that wrote a row answered 500 (#196).
- **The roadmap's worry about Vercel's function timeout does not arise**, and the build log says why rather than
  raising a timeout: no function is in the path of a plan (#206).

## What was built

```
src/backend/Dockerfile                 ← the image that is deployed: pinned installs, not root, APP_ENV=production;
                                         checks its settings, migrates, then `exec`s the server on $PORT
railway.json                           ← NEW: how Railway builds it, health-checks it (GET /ready) and stops it (70 s)
constraints.txt                        ← NEW: the exact versions the suites run on; the image and CI install with it
src/backend/app/preflight.py           ← NEW: what a deployment is missing, all of it, by name, before it starts
src/backend/app/db/url.py              ← NEW: a hosted Postgres's URL as asyncpg and as psycopg2 need it;
                                         nothing prepared is kept, so a pooler can sit in between
src/backend/app/db/session.py          ← the engine made from it; connections tried before use, kept five minutes
migrations/env.py                      ← migrations to DATABASE_URL_UNPOOLED when one is given
src/backend/app/db/redis.py            ← a closed connection is retried, an idle one pinged, a silent one probed
src/ai/mcp_server/cache.py             ← the same for the tools' cache — and no password in its log line
src/backend/app/core/error_reporting.py ← NEW: Sentry, on with a DSN; the scrubber; a run's reports tagged with its
                                         trip; the access log without login tokens
src/backend/app/api/routes/admin.py    ← GET /admin/sentry-test: fails on purpose, for whoever holds OPS_TOKEN
src/backend/app/api/routes/health.py   ← GET /ready: /ping's findings as a status; short errors in production
src/backend/app/api/routes/trips.py    ← a run keeps its note up, registers itself, and is tidied up after when it
                                         is cut short; the event stream waits in pieces
src/ai/utils/conversation.py           ← the run note lives 45 s and is renewed every 15
src/backend/app/main.py                ← preflight and Sentry at startup; recovery that leaves another instance's
                                         runs alone and looks again; shutdown that waits for the plans in flight;
                                         Cache-Control: no-store; a 500 in the usual envelope; CORS by pattern
src/backend/app/core/config.py         ← DATABASE_URL_UNPOOLED, CORS_ORIGIN_REGEX, SENTRY_DSN,
                                         SENTRY_TRACES_SAMPLE_RATE, OPS_TOKEN, SHUTDOWN_GRACE_SECONDS
src/backend/app/__init__.py            ← the version, in one place: 0.27.0
requirements.txt                       ← sentry-sdk
.github/workflows/ci.yml               ← installs with constraints.txt: CI tests what ships
.env.example                           ← Sentry, and a Production section

src/frontend/next.config.mjs           ← a deployment build stops without the backend's two addresses
src/frontend/src/lib/sse.ts            ← the stream's address without a trailing slash
src/frontend/playwright.production.config.ts, e2e-production/smoke.spec.ts
                                       ← NEW: one check in a real browser of a site that is already running

docker/prodlike/                       ← NEW: the production-like stack (compose.yml says what each part is for)
scripts/prodlike.py                    ← NEW: up / down / logs for it
scripts/prod_check.py                  ← NEW: the roadmap's acceptance, asked of a running deployment from outside
scripts/sentry_receiver.py             ← NEW: a local stand-in for Sentry — prints each report, shows what it holds
scripts/freeze_constraints.py          ← NEW: writes constraints.txt again from the environment it is run in

tests/conftest.py                      ← error reporting is off before and after every test
```

## The path of a request, deployed

```
browser ──── https://<site>.vercel.app ──────────────────────────────────────────────┐
   │   pages: static, or rendered by a function that calls nothing                    │
   │                                                                                  │
   ├─ /api/* ──▶ Vercel's proxy (a rewrite; waits 120 s for a first byte) ──▶ Railway ─┤  Cache-Control: no-store
   │                                                                                  │
   └─ EventSource ──────────────── straight to ───────────────────────────▶ Railway ──┘  CORS_ORIGINS must name the site
                                   /trips/<id>/stream?token=…

Railway: one container of src/backend/Dockerfile        PORT, SIGTERM, 70 s to drain
   python -m app.preflight      ← every missing or unsafe setting, at once; exit 1 in production
   alembic upgrade head         ← DATABASE_URL_UNPOOLED if given
   exec uvicorn …               ← process 1
        │
        ├── asyncpg ── TLS ──▶ Neon's pooler (PgBouncer, transaction mode) ──▶ Postgres + pgvector
        │                      nothing prepared is kept between transactions
        ├── redis-py ─ TLS ──▶ Upstash, over TCP           commands: retried when the connection was closed
        │                                                  SUBSCRIBE: reconnects and subscribes again
        ├── tool server (a subprocess; inherits the environment) ──▶ Duffel · LiteAPI · OpenTripMap · OpenWeather
        └── sentry-sdk ──────▶ Sentry                      scrubbed: by value, by name, no locals, no bodies

POST /plan → 202 at once; the run is a task of this process:
   note in Redis (45 s, renewed every 15)  ← "someone is planning this trip"
   a new instance starting                 → leaves a trip with a note alone; looks again every 45 s
   SIGTERM                                 → waits up to 40 s for its runs; gives up the rest, saying why
```

## Dev A — the backend's deploy

| Roadmap | What was done | Where it is held |
|---|---|---|
| Backend → Railway: Dockerfile deploy | The Dockerfile listens on `$PORT`, `exec`s the server, installs pinned versions, runs as an unprivileged user; `railway.json` names it, the health check and the draining time. **Not deployed** — started as a platform starts it on the stand-in | `test_the_image_checks_migrates_and_then_becomes_the_server`, `test_the_platform_is_told_how_to_build_check_and_stop_it`; the stack's first start |
| Document every missing env var found on first deploy | The old code deployed first, six attempts recorded; then `app.preflight`, which says them all at once and refuses the unsafe ones in production (#191) | the first-deploy table below; 20 tests of the check |
| Sentry: `sentry_sdk.init(dsn=…, traces_sample_rate=0.1)` | As written, plus environment and release; off without a DSN and in tests; reports scrubbed (#203, #204) | `test_with_a_dsn_it_is_on_as_the_roadmap_says_…` and 25 more |
| Deliberately raise an exception on a test endpoint and verify it appears in Sentry | `GET /admin/sentry-test`, behind `OPS_TOKEN`. Verified to arrive, over HTTP, at a server that receives it — **not in Sentry itself** | `test_the_deliberate_error_is_reported`; integration over real sockets; `prod_check.py` check 4 |
| Verify pgvector on Neon: `SELECT * FROM pg_extension WHERE extname = 'vector'` | `prod_check.py` runs exactly that against a deployment's database, and checks its version (0.8+ is needed by the search), the migration and the HNSW index. On the stand-in the migrations created the extension on an empty database. **Not run against Neon** | `prod_check.py` check 2 |

## Dev B — the frontend's and Redis's deploy

| Roadmap | What was done | Where it is held |
|---|---|---|
| Frontend → Vercel: function timeout — "planning takes 15–30s, default 10s will fail" | Nothing to raise: `POST /plan` answers at once, the progress is a stream the browser opens straight to the backend, and `/api/*` is Vercel's proxy, not a function (#206). What can go wrong is the two addresses compiled in at build time: a deployment build now stops without them. **Not deployed to Vercel** — built with `next build`, served with `next start` | `test_a_deployment_build_of_the_frontend_stops_…`; `prod_check.py` checks 5 and 6 (every proxied request timed: the slowest 0.4 s) |
| Redis → Upstash: REST does not support pub/sub, must use TCP. Document | `rediss://` over TCP is the only form accepted; a REST address stops a production start by name (#198). Closed connections cost nothing (#199) | preflight's tests; `test_a_closed_connection_is_retried_…`; integration on a real Redis; the stand-in |
| Run the full planning flow against production end-to-end | Run against the production-like stack with the real providers and models: script (29 checks) and browser. **Not against Vercel → Railway → Neon + Upstash** | Live results, below |

## Found and fixed along the way

1. **Registering an account answered 500 in the image** — and only there. `requirements.txt` has lower bounds; the
   image's fresh install had SQLAlchemy 2.1.4 and sqlmodel 0.0.48, and that sqlmodel refuses the naive timestamps
   every model writes. Every test had run on 2.0.51 and 0.0.39. Now the image and CI install the tested versions
   (`constraints.txt`), and a test fails when the two drift apart (#196).
2. **A hosted Postgres's URL could not be used at all.** With it, the app started and every request failed
   (`unexpected keyword argument 'sslmode'`); rewritten for asyncpg, the migrations failed instead (#192).
3. **Through a pooler, the second transaction failed.** Prepared statements were kept on connections the pooler
   had already given to someone else (#193).
4. **The server never heard "stop".** The start command made a shell process 1; the server was a child that got
   no signal, and would have been killed mid-plan on every deploy (#195).
5. **It listened on port 8000 whatever port it was given** (#195).
6. **A health check on `/ping` would have put a broken deploy live:** 200, with the error in a body nobody reads (#197).
7. **Every deploy would have failed the plans being made.** The new instance's startup marked every "planning"
   trip failed while the old instance was still planning them (#201).
8. **A run cut off by a stop left its trip "planning" for good.** Only a crash was tidied up after, not a
   cancellation; every later request for that trip was a 409 (#202).
9. **The tools' cache wrote Redis's password into the log** on every start (#198).
10. **The server's access log wrote every event stream's login token** (`GET /trips/<id>/stream?token=…`) (#204).
11. **A Redis connection closed by the server was one failed request each**; for the event stream, the end of the
    stream. The library's defaults are no retry and no health check (#199).
12. **`python -m app.preflight --env-file …` judged the shell, not the file.** Settings read the environment before
    any file, so with `DATABASE_URL` exported in the terminal the file's own value was never looked at. Found
    because the break-on-purpose run exports one; the file is now judged on its own.
13. **The scrubber took the release for an e-mail address.** `tripplanner-ai@0.27.0` reached the stand-in as
    `[Filtered]`. An address ends in letters now.
14. **My own comment said the event stream would end when its Redis connection died; the stand-in showed it
    heals.** With the retry in place the library reconnects and subscribes again, and an event published a
    second after the kill reached the page. The comment was corrected to what happens.
15. **`README` and `HOW_TO_RUN` told a new clone to `pip install -r requirements.txt`** — which is finding 1, on
    a laptop. Both install with the constraints now.

## Live results (the production-like stack, 2026-10-11)

The stack: `docker/prodlike/compose.yml`, started by `python scripts/prodlike.py up` — the image that is deployed,
given its settings as environment variables only and a port to listen on; a Postgres + pgvector that refuses any
connection without TLS and starts empty; PgBouncer in transaction mode in front of it, wiping the server
connection after every transaction; a Redis reachable only over TLS, with a password, that closes idle
connections; `scripts/sentry_receiver.py` in place of Sentry; and the frontend built for production
(`next build`, `next start`) with the backend's address compiled in. The providers and the models are the real ones.

### `python scripts/prod_check.py --prodlike` — 29 passed, 0 failed

```
1. The backend answers  (http://localhost:8200)
  ✓ GET /ready is 200 — 200 {'postgres': 'ok', 'redis': 'ok'}
  ✓ answers say Cache-Control: no-store — cache-control: 'no-store'
  ✓ it is this version of the code — deployed 0.27.0, here 0.27.0

2. pgvector on the database
   localhost:6543/tripplanner
  ✓ SELECT … FROM pg_extension WHERE extname = 'vector' finds it
  ✓ pgvector is 0.8 or newer — version 0.8.6
  ✓ the schema is at the newest migration — database 006, code 006
  ✓ the similarity index (HNSW) exists — ix_embeddings_summary_hnsw
  ✓ the connection is encrypted

3. Redis pub/sub over TCP
   rediss://localhost:6390
  ✓ PING over TCP with TLS
  ✓ a published message arrives at a subscriber

4. A deliberate error reaches Sentry
  ✓ without the operations token the route is not there — 404
  ✓ with it, the route fails on purpose — a 500 that says nothing of what went wrong
  ✓ the report arrived at the stand-in — marker check-950c8fd0a77d
  ✓ it is the error that was raised — DeliberateError: deliberate test error check-950c8fd0a77d
     environment 'production', release 'tripplanner-ai@0.27.0'
  ✓ nothing secret in anything the stand-in was sent (201,593 characters, 13 values looked for)

5. The frontend answers  (http://localhost:3200)
  ✓ its page loads — 200
  ✓ /api/* on its origin reaches the backend — 200 {'postgres': 'ok', 'redis': 'ok'} in 13 ms
  ✓ what comes back through it is not cached — cache-control 'no-store'

6. A trip is planned end to end  (requests through http://localhost:3200/api, the event stream straight to http://localhost:8200)
  ✓ an account is made
  ✓ it can sign in — 200
  ✓ a trip is created — 201
  ✓ the event stream opens, straight from the backend — 200
  ✓ the backend lets a page on the frontend's origin read it (CORS) — access-control-allow-origin: 'http://localhost:3200'
  ✓ POST /plan answers at once — 202 in 20 ms
  ✓ the plan arrives over the stream — 9.9 s; 40 events (35 pieces of the itinerary as it was written); ended with 'planning_complete'
  ✓ GET /status says completed
  ✓ the itinerary is saved
     4 days, ₹29,036
  ✓ it exports as a PDF — 564,227 bytes
     every request that went through the frontend:
           224 ms  POST /auth/register
           205 ms  POST /auth/login
            29 ms  POST /trips
           429 ms  GET /trips/<id>/export/pdf
            45 ms  GET /trips/<id>/status
            22 ms  GET /trips/<id>/itinerary
            20 ms  POST /trips/<id>/plan
  ✓ none of them comes near the 120 s the frontend's host allows — the slowest, GET /trips/<id>/export/pdf, took 0.4 s
```

### The image's first start, on a database nobody had migrated

```
[PREFLIGHT] APP_ENV=production
Every setting a deployment needs is there.
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema — users, trips, itineraries, agent_runs, embeddings
…
INFO  [alembic.runtime.migration] Running upgrade 005 -> 006, What each run asked of the models — Phase 26 token and cost tracking
INFO:     Started server process [1]
INFO:     [STARTUP] production — database pooler:6432/tripplanner, Redis rediss://upstash-standin:6379
INFO:     [SENTRY] error reporting is on — environment 'production', release 'tripplanner-ai@0.27.0', 10% of requests traced
INFO:     [TRACING] LangSmith tracing is off (LANGCHAIN_TRACING_V2 is not true)
INFO:     Uvicorn running on http://0.0.0.0:8200 (Press CTRL+C to quit)
```

Process 1 is uvicorn (so a platform's stop signal reaches it); the port is the one the "platform" named; the
migrations made the extension and the schema with no `init.sql`; neither URL's password is in the log.

### What a deploy does to a plan in flight

| What was done | What happened |
|---|---|
| **A second instance started while the first was planning** — the overlap every deploy has | The new one logged `[STARTUP RECOVERY] 1 trip(s) are being planned by another instance — left alone, looked at again in 45 s`. The plan completed on the first, 7 s later; asked of the second, the trip said `completed`. |
| **The planning instance was stopped politely** (SIGTERM, 1 s into a plan) | It logged `[SHUTDOWN] 1 planning run(s) in flight — waiting up to 40 s for them`, finished the plan, and exited with code 0, 29 s after it was asked to. (The plan was saved after about 8 s; the rest was the run's last step — learning the traveller's preferences — held up by the model provider's per-minute limit after two plans in a row.) Asked of the other instance: `completed`. |
| **The planning instance was killed** (SIGKILL, 2 s into a plan) **and restarted** | The trip stayed `planning` while its note in Redis lived. The restarted instance found the note, left the trip alone, looked again 45 s later — `[STARTUP RECOVERY] 1 run(s) died with the instance that had them` — and the trip was `failed`, with the reason for the page: "The server was restarted while this trip was being planned. Nothing is lost — please start it again." |

### What a hosted Redis does to connections

| What was done | What happened |
|---|---|
| **Every ordinary connection killed in Redis** (`CLIENT KILL TYPE normal` — what a redeployed proxy does) | The next three requests that needed Redis answered 200: a command that finds its connection closed is sent again on a new one. |
| **The connection under an open event stream killed** (`CLIENT KILL TYPE pubsub`) | The stream stayed open. An event published 1 s after the kill reached the page, as did those 6 s and 16 s after: the subscription had connected and subscribed again by itself. |

### In a real browser, and with the cross-origin setting broken on purpose

`npx playwright test -c playwright.production.config.ts` — Chromium, the production build on :3200, one real plan:

```
planned on http://localhost:3200; its event stream came from http://localhost:8200; …
  ✓  a traveller plans a trip on the deployed site (12.6s)
```

Every request but the stream went to the site's own `/api/*`; the stream went straight to the backend; the
browser's console had no complaint about either. Then the setting that decides the second half, broken two ways:

| The backend's `CORS_ORIGINS` | What happened |
|---|---|
| only `http://localhost:3200`, and no pattern | The container exited with code 1 before the migrations: `Cannot start in production: ✗ CORS_ORIGINS names no deployed frontend: the page opens its event stream straight from the browser, and the browser will refuse it.` |
| another site's origin (`https://elsewhere.example`) | It started. `prod_check.py`: **28 passed, 1 failed** — `✗ the backend lets a page on the frontend's origin read it (CORS) — access-control-allow-origin: None`. The browser check failed where a traveller would have noticed: `[data-stream="connected"]` never appeared. The plan itself was still made — the page's polling of `GET /status` carried it — which is exactly why this mistake survives a quick look. |
| put right | the browser check passes again (16.4 s) |

### The first deploy of the code as it was

The roadmap asks for "every missing env var found on first deploy". So the code from before this phase was
deployed first: an image built from commit `5f98d58` — its own Dockerfile, dependencies installed fresh, as a
platform builds it — started on the same stack with a port to listen on, and given one setting at a time.

| Attempt | What it did |
|---|---|
| **1. Nothing set but the port** | Exited. `connection to server at "localhost", port 5432 failed: Connection refused` — the first missing variable, `DATABASE_URL`, said as a stack trace from the migrations. |
| **2. + `DATABASE_URL`, as a hosted Postgres hands it out** (`?sslmode=require&channel_binding=require`) | The migrations ran — psycopg2 is libpq and reads that URL. Then the app **started, and stayed up, broken**: `[STARTUP RECOVERY] skipped: connect() got an unexpected keyword argument 'sslmode'`. asyncpg has no such parameter, so no request could reach the database — and `GET /ping` answered **200** saying so in its body, which a platform's health check does not read. It was also listening on port 8000, not on the port it had been given: nothing answered there. |
| **3. + the URL rewritten the way asyncpg wants it** (`?ssl=require`) | Exited. `psycopg2.ProgrammingError: invalid dsn: invalid connection option "ssl"`. No one URL served both drivers. |
| **4. Its settings with nothing given** | `APP_ENV=development` (so every SQL statement, with its parameters, goes to the log), `JWT_SECRET` the default that is in the repository, `CORS_ORIGINS=http://localhost:3000`. None of it stops the app from starting. |
| **5. The first row written** | `StatementError: Datetime values must have timezone information`. Nothing to do with a variable: the fresh install had brought SQLAlchemy 2.1.4 and sqlmodel 0.0.48, where every test had run on 2.0.51 and 0.0.39. Registering an account answered 500. |
| **6. The tools' cache connecting to a hosted Redis** | It connected — and wrote the whole URL to the log, password included: `MCP cache: Redis connected at rediss://default:<the password>@…`. |

And through the pooler, the same query in ten transactions, with the database engine set up as it was and as it is:

```
the engine as it was (statements prepared once and kept): failed on transaction 2 —
    InvalidSQLStatementNameError: prepared statement "__asyncpg_stmt_…__" does not exist
the engine as it is now (nothing kept)                  : 10 of 10 transactions answered
```

Every row is something this phase changed, and `DECISIONS.md` #191–#207 say how. What a platform must be given is
now one list, said in one message before anything starts: `python -m app.preflight`.

### What one plan costs a Redis that bills by the command

Counted on the stand-in (`INFO commandstats`) around one trip created — its opening searches warmed — and planned:

| Commands | |
|---|---|
| 33 | `PUBLISH` — the plan's events: the searches' progress and the itinerary as it is written, in pieces |
| 17 | `GET` — the tools' cache, the run note, the planning state |
| 5 | `SET` — cache entries, the note, the state, the history |
| 2 | `INCRBY`, 1 `DEL`, 1 `PING` |
| 13 | connection set-up (`HELLO`, `AUTH`, `CLIENT SETINFO`) for the four connections opened |
| **73** | **in all** — about 60 of them the plan's own |

At 500,000 commands a month on a free plan that is some 7,000 plans. A page left open on a trip costs one
`SUBSCRIBE`, and nothing while nothing is published; a run in flight renews its note every 15 s (one `SET`).

**A quiet minute.** The stand-in closes connections idle for 60 s. After 75 s in which nothing was asked, the
app had sent Redis nothing at all, and the first request that needed Redis answered 200 in 0.11 s — on a
connection it had to open again.

## Tests

**980 unit + contract, 60 integration, 60 browser — all passing.** New in this phase: 80 unit, 9 integration, and one browser check that
is not part of the everyday suite (it needs a running deployment).

| File | Tests | What they hold |
|---|---|---|
| `tests/unit/test_phase27_production.py` | 40 | **The database URL**: Neon's, as handed out, translated for asyncpg and left whole for psycopg2; the old way fails on `sslmode` without touching a network; nothing prepared is kept; other libpq parameters translated or left out by name; nothing logged with credentials. **Preflight**: a complete deployment is told so; the repository's defaults do not start in production, with every reason in one message; each of twelve things that stop a deployment, in words; what only degrades; no value ever printed; a file judged on its own, whatever the shell has exported; the app refuses before it connects to anything. **Redis**: a closed connection retried, a timeout not; health checks and keep-alive; no password in a log. **Answers**: `/ready`'s status against `/ping`'s body; short errors in production; `no-store` unless a route says otherwise; the deliberate error only with the token; a 500 in the envelope, saying nothing. **The files**: every requirement pinned and this environment the pinned one; the image's start command, step by step; `railway.json` against the app's routes and its own shutdown times; the stand-in gives the backend what a platform would; every setting in `.env.example`; a deployment build of the frontend stops without the backend's address |
| `tests/unit/test_phase27_error_reporting.py` | 26 | Off without a DSN and in tests; on as the roadmap writes it; switched on at startup and flushed at shutdown, also when something else fails to close. **The deliberate error is reported**, once, unhandled, with environment and release. **What does not leave**: the login token in an address, the operations token in a header, a cookie, a body — in the error and in the trace of the same request; every secret the process holds, wherever it turns up, including in a log line; no local variables; a provider's key in a tool's request address (the SDK's real breadcrumb of a real client call); the client's address in an access-log breadcrumb. A run's reports carry its trip and leave nothing on the next. The scrubber, text by text — and what it leaves alone; by name as well as by value; which secrets it looks for; a report that cannot be scrubbed is not sent. The access log without tokens |
| `tests/unit/test_phase27_runs.py` | 14 | A note lives three beats; a run keeps it up, also past a beat that fails, and stops when it ends. **A run cut short** does not leave its trip planning — failed, or completed if a plan stood — tells the page, writes the reason, clears its note; a trip that was already over is not touched; a crash is still tidied up, and reported under its trip. **A stopping server** waits for its plans, gives up the ones that do not finish, in that order before closing what they need. A starting instance can tell which trips are alive, and takes none to be when Redis cannot say. **The event stream** waits in bounded pieces and ends quietly when Redis cannot be reached again; a page loaded later is told why a run failed; an interrupted run is where a run ends — the next one is not shown its searches or its reason |
| `tests/integration/test_phase27_production_integration.py` | 9 | On real Postgres and Redis: a URL with a hosted Postgres's parameters connects through both drivers, the same statement a dozen times; migrations go to the direct address when given; a starting instance fails the dead run, leaves the live one, completes the interrupted change, writes one reason; an instance that dies later is noticed; **over HTTP, a plan given up by a stopping server says why and can be started again**; `/ready` on real services, with Redis taken away; a connection killed in Redis costs the next command nothing (and with the library's defaults, one failure); a subscription whose connection is killed goes on hearing; an error report arrives at a server over real sockets with no secret in it |
| `src/frontend/e2e-production/smoke.spec.ts` | 1 | In Chromium against a running site: an account, a trip, the event stream open across origins, the plan arriving, where each kind of request went, nothing refused in the console, the plan still there after a reload |

Changed: `test_phase20_streaming.py` (the note's lifetime is 45 s, not an hour).

What was run on the final code, 2026-10-11:

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check` | clean |
| `pytest tests/unit tests/contract` | 980 passed |
| `RUN_INTEGRATION=1 pytest tests/integration` | 60 passed |
| `npx playwright test` (the everyday suite, stub backend) | 60 passed |
| `npx tsc --noEmit`, `npm run lint`, `next build` | clean; the production build completes |
| CI's own steps in a clean `python:3.11-slim` container, with no `.env`, installing with `constraints.txt` | lint clean; 979 passed, 1 skipped (the frontend's build guard needs node, which that image has not) — on SQLAlchemy 2.0.51 and sqlmodel 0.0.39, the pinned versions, under Python 3.11 |
| the image, built and started on the production-like stack | starts on an empty TLS-only database; `prod_check.py --prodlike` 29 of 29 |
| the same in Chromium | passes; fails when the origin is wrong; passes again |
| `alembic check` | no drift between the models and the migrations |
| breaks made on purpose, on a copy | 98 of 98 caught |

### Break it on purpose

Ninety-eight breaks, each applied to a copy of the repo, one line at a time, and the tests run against it —
the unit and contract suites and this phase's integration tests, on a database of the copy's own, checked after
every break. **All ninety-eight are caught**, none by a hang, by forty-nine different tests; three of them by
tests of earlier phases (a laptop refused its defaults breaks the app's startup test; a route's own cache header
overwritten breaks the PDF download's; the note's lifetime is also pinned by Phase 20's).

The run earned its keep before the first break. Its baseline — the tests on the *unbroken* copy — failed: it
exports a `DATABASE_URL` for its private database, and with one exported, `preflight --env-file` judged the
shell's value instead of the file's. That is finding 12, and it would have misled exactly the person the tool is
for: someone checking `.env.production` from a terminal that has other values in it.

And the system was broken on purpose too, where only a running stack can show the result: the cross-origin
setting two ways (above), every Redis connection killed, the planning instance stopped and then killed, and the
code as it was deployed onto the stand-in — six failures, each of which a row below now holds.

| Break | Caught by |
|---|---|
| `sslmode` handed to asyncpg as it stands / asyncpg keeps its statement cache / SQLAlchemy keeps its prepared statements / every statement has the same name / the engine made without the translated arguments / connections kept past five minutes / not tried before use | the URL's and the engine's tests |
| `connect_timeout` not translated / `options` and `application_name` dropped / an unknown parameter passed on / any `sslmode` passed on / `postgres://` not understood / Alembic's URL loses its parameters / a database logged with its password | the same |
| Migrations ignore the direct address | integration: Alembic, with the pooled address leading nowhere |
| A timed-out command is sent again too / a closed connection is not retried / an idle one not checked / no keep-alive probes / a Redis logged with its password / the tools' cache logs its URL, or does not retry | the Redis tests |
| The default signing secret accepted / a short one / a REST address for Redis / plain `redis://` to Upstash / a database on localhost / a missing model key only degrades / a pattern of origins does not count | each thing that stops a deployment, in words |
| Production starts whatever is wrong / a laptop is refused its defaults / `enforce` never refuses / the app starts without asking / a file is judged together with the shell | preflight's tests; the app refuses before it connects |
| Local variables sent / request bodies sent / personal data sent by default / every request traced | on as the roadmap says, carrying nothing it was not asked to |
| Errors not scrubbed / breadcrumbs not / traces not / secrets not taken out by value / credentials stay in a URL / a login token stays in text / e-mail addresses stay / a parameter named like a secret stays / a value not judged by its name | what must not leave — each by the test that puts that thing in reach |
| A report that cannot be scrubbed is sent as it is / the access log keeps the token / a breadcrumb keeps the client's address | their own tests |
| Reporting on in the test stacks / the app never switches it on / the last reports not sent at shutdown / the tool server's keys not looked for / the access log not quietened | on, off, and the app's startup and shutdown |
| A run not tagged with its trip / its tags stay on whoever reports next / a crashed run reported without its trip | a run is reported under its trip |
| `/ready` always 200 / production shows the whole error | the health tests |
| A route's own cache header overwritten / answers do not say `no-store` / a 500 says what went wrong | their own tests, and Phase 19's PDF download |
| Anyone may make the app fail on purpose / with no token configured, no token opens the door | the route that fails on purpose |
| A starting instance fails every trip it finds planning / a trip left alone is never looked at again / a failed trip is not told why / an interrupted change is failed rather than left as its plan | integration: an instance that starts beside another |
| A run that died later is never marked | integration: an instance that dies after all |
| With Redis down every run counts as alive | a starting instance can tell |
| A stopping server does not wait for its plans / closes Redis first / gives its plans no time / does not give up the ones that do not finish | a stopping server |
| A run does not keep its note up / goes on writing it when it is over / the server does not know which plans it is making / a note lives for an hour | the note is the run's sign of life (and Phase 20's test) |
| A cancelled run is not tidied up after / the page is not told / no reason is left / a finished trip is marked failed by a late cancel / a given-up change loses its plan | a run that is cut short |
| A page is not told why an interrupted run failed / an interrupted run is not where a run ends | the page, afterwards |
| A lost Redis ends the event stream with an error / the stream's subscription is left open | the event stream |
| The server is not the process that is signalled / it listens on 8000 whatever it is told / installs the newest of everything / runs as root / starts without checking its settings / is development by default | the image, step by step |
| The platform checks `/ping` / kills before the plans are done | the platform is told how |
| A deployment build goes ahead without the backend's address | the frontend's build guard (needs node) |
| A requirement is left to float | what is deployed is what was tested |

## Done criterion checklist

| Roadmap | Status |
|---|---|
| Backend → Railway: Dockerfile deploy | ⚠️ The image, its start, its health check and its stop are built and run on the stand-in. **Not on Railway** |
| Document every missing env var found on first deploy in `DECISIONS.md` | ✅ #191, from a first deploy of the old code onto the stand-in; and the app now says them itself |
| Sentry: `sentry_sdk.init(dsn=settings.SENTRY_DSN, traces_sample_rate=0.1)` | ✅ |
| Deliberately raise an exception on a test endpoint and verify it appears in Sentry | ⚠️ Raised, reported, received over HTTP by a server and read back — **not in Sentry** |
| Verify pgvector extension on Neon | ⚠️ The query is `prod_check.py`'s check 2 and passes on the stand-in's Postgres — **not run on Neon** |
| **Acceptance A:** backend accessible at Railway URL; one deliberate test error appears in Sentry; pgvector confirmed on Neon | ⚠️ **None of the three on the real service.** Each on its stand-in |
| Frontend → Vercel: check function timeout | ✅ answered by measurement and by how the app works (#206) — ⚠️ **not deployed to Vercel** |
| Redis → Upstash: TCP, not REST; document in `DECISIONS.md` | ✅ #198, #199 — ⚠️ against a TLS Redis with a password, **not Upstash** |
| Run full planning flow against production end-to-end: Vercel → Railway → Neon + Upstash → real APIs | ⚠️ production build → deployed image → TLS Postgres behind a pooler + TLS Redis → **real APIs**: yes. The four hosts: no |
| **Acceptance B:** full end-to-end flow works in production; SSE via Upstash TCP (not REST); Vercel function timeout not hit | ⚠️ **On the stand-in**: the flow works, the stream runs on pub/sub over TCP with TLS, no request through the frontend took longer than 0.4 s |

## Known limits

- **Not deployed.** Everything marked ⚠️ above. What the stand-in cannot know is listed under "The deploy itself".
- **One server process.** A run is a task of the process that started it; the rate limiters and the tool server's
  session are that process's too. Railway must run one replica. Two would each plan correctly — the event stream
  and the one-run-a-trip rule go through Redis and Postgres — but each would keep its own provider rate limits.
- **A run does not move.** A plan cut off by a kill is failed, with a reason, and the traveller starts it again;
  nothing resumes it. A queue would, and is not built.
- **Events published while a subscription reconnects are lost** — a second or so. The page's status polling
  covers the outcome, not the missed pieces of a live draft.
- **A silently dead connection takes about a minute to notice** (keep-alive: 30 s + 3 × 10 s). Not shown on the
  stand-in: it cannot drop packets on request. That is Phase 32's subject.
- **The tool server's own crashes are not reported to Sentry.** It is a subprocess; what reaches Sentry is the
  app's side of the failure. Its keys are scrubbed from the app's reports all the same.
- **Sentry gets source lines.** A report's stack frames carry the lines of code around each call, as the SDK
  does by default. The code is this repository's and public; no value of any variable goes with it.
- **`/admin/embedding-health` and `/admin/cost-summary` are still open to any signed-in user.** Only the route
  that costs a third party's quota got the operations token.
- **The upgrade is owed.** `constraints.txt` holds the project on the versions it was tested on. The newest
  sqlmodel will not write its timestamps; moving there means making them aware, or declaring them naive.
- **A check leaves an account behind** in whatever it checks (`prod-check-…@example.com`,
  `browser-check-…@example.com`), with one trip. There is no route to delete an account.
- **The stand-in's certificates are its own.** Its Redis is trusted through a CA file in the URL; a hosted one is
  signed by a public authority, and that path — the default one — is the one not exercised here.
- **`/ready` is asked once**, when a deployment starts: Railway does not use it afterwards. A database that
  goes away later is `/ping`'s to show, and Sentry's.
- **Migrations run while the old version still serves.** One that the old code cannot live with will break it
  for the length of the overlap.

## The deploy itself

Five accounts and five values (`HOW_TO_RUN.md`, Step 25 B, has every click): Neon's pooled URL, Upstash's
`rediss://` URL, a Sentry DSN, and the two places the code runs — Railway for the image, Vercel for
`src/frontend`. Then `python scripts/prod_check.py --api … --web … --env-file .env.production` and the browser
check ask the real thing what was asked of the stand-in.

What to expect to differ, because no stand-in can know — each is a place the first real run may find something:

| The stand-in | The real service |
|---|---|
| PgBouncer that tracks no prepared statements and wipes a connection after every transaction | Neon's pooler does track them (`max_prepared_statements`). The app keeps none either way |
| `channel_binding=require` works only on the direct address: plain PgBouncer cannot do it, so the stand-in gives Alembic `DATABASE_URL_UNPOOLED` | Neon hands out `channel_binding=require` on the pooled address too. If Alembic is refused there, set `DATABASE_URL_UNPOOLED` |
| Postgres is always awake | Neon suspends an idle database; the first request after a pause waits for it to wake. The pool tests a connection before using it — whether that first request is slow or fails is not known |
| pgvector 0.8.6 | whatever Neon has; `prod_check.py` fails below 0.8, which the search needs |
| Redis closes a connection idle for 60 s, with a FIN | Upstash: after an hour, or when its proxy is redeployed — possibly without a word. That is what the keep-alive probes are for, and they are untested |
| a certificate from the stand-in's own CA | a public one; `rediss://` verifies it with no extra setting |
| `docker stop`: SIGTERM, 70 s, SIGKILL | Railway's `drainingSeconds: 70` should be the same; whether it honours 70 is not known |
| the port in `PORT`, a second instance started by hand | Railway's own overlap of old and new, 30 s by `railway.json` |
| `next start` serving the rewrite itself | Vercel's proxy: 120 s to a first byte by its documentation, and a CDN that now honours an origin's `Cache-Control` — `prod_check.py` looks at `x-vercel-cache` |
| a receiver that accepts anything | Sentry: the DSN accepted, the event grouped, the quota. `prod_check.py` reads it back through Sentry's API when given a token |
