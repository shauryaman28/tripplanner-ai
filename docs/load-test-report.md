# Load test report — Phase 30

**Status: done, on one machine.** The roadmap's Phase 30: a baseline before any tuning, the bottleneck
found and fixed, and the same test run again. Measured 2026-10-12 and -13.

> Everything here was measured on **one laptop that ran both the load generator and the system under
> load** (Apple silicon, 10 cores, on battery; Docker Desktop with 8 GB), against **stand-ins** for
> the models and the providers. There is no staging and no deployment. The figures say where this
> app runs out first and what a change does to that. They are not what a deployment will do.

## 1. In short

| | Visits a second it holds | Plans a second | Requests a second | Request p99 | A plan, p95 |
|---|---|---|---|---|---|
| **Before** (as Phase 29 left it) | **0.1** | 0.11 | 1.3 | 210 ms | 13.2 s |
| After the configuration change | 1.6 | 1.61 | 20.8 | 199 ms | 14.0 s |
| **After** all three changes | **3.2** | 3.21 | 43.0 | 206 ms | 15.4 s |

- **Before:** 1.3 requests a second at 210 ms p99 was the most it held — one visit every ten seconds.
  At one visit every five seconds the p99 was 1.3 s; at 0.8 visits a second it was 15.8 s, a plan took
  two minutes, and two travellers in three got no plan at all.
- **The bottleneck:** database connections. Every plan in flight held one — idle, inside a
  transaction — for its whole 12 seconds, and the stack had 4 at its pooler and 15 in the app. The
  processor was never busier than a sixth of one core.
- **The configuration change:** the pooler lends 90 connections (what the hosted pooler it stands
  in for lends) instead of 4, and the app's pool is a setting, 40 here, instead of the library's 15.
  Nothing else changed: **20.8 requests a second at 199 ms p99**, sixteen times the visits.
- **Then the cause:** the code no longer leaves a transaction open while it waits for a model. With
  the baseline's own 4 + 15 connections put back, the rates that had broken the baseline held. And
  the second wall that the first had been hiding — the Redis library's limit of 100 connections,
  one per open page — became a setting too.
- **After:** **43.0 requests a second at 206 ms p99** — 3.2 visits a second, every plan made, in 13
  seconds. Thirty-two times the baseline.
- **What limits it now:** one process on one core. At 6.4 visits a second the backend is at 0.93 of
  a core and stops coping. Half of that core is spent checking passwords (section 11).

How each of those is known is the rest of this document; every run is a file in
[loadtest/](loadtest/), and the tables below are printed from those files
(`python scripts/loadtest.py table baseline more-connections fix-on-baseline-pools after`).

## 2. What was under load

**The image that is deployed**, on the production-like stack of Phase 27 (`docker/prodlike/`): one
uvicorn process, its tool server as a subprocess, a TLS-only Postgres behind PgBouncer in transaction
mode, a TLS-only Redis. Everything of the app is real. What is not real is what the app calls:

**Stand-ins for the models and the providers.** A planning request asks three models and five
providers, and none can take load — Gemini's free tier is about twenty requests a day, Groq allows
one plan's tokens every few seconds, the flight and hotel providers are sandboxes. So stand-ins
answer for them (`loadtest/standins/`):

| What is stood in for | Where | What stays real |
|---|---|---|
| The chat models (`ChatGroq`, `ChatGoogleGenerativeAI`) | the class the planner instantiates | the planner's own calling code, LangChain, the callback that meters tokens, counts `llm_calls_total` and makes the span |
| The embedding API | the one function that reaches it | the embedding job, its rows, pgvector |
| Duffel, LiteAPI, OpenTripMap, OpenWeather, Nominatim | `httpx.get` / `httpx.post` inside the tool server's own process | the tools, their Redis cache and its keys, one search per key at a time, the rate limits, the backoff, the worker threads |

**They cannot be switched on in a deployment, because there is no switch.** No setting, flag or
environment variable turns them on — a flag gets set by accident. They are a different program:

1. they live outside `src/`, and the deployed image copies `src/`, the migrations and nothing else:
   the code is not there;
2. the load test's image is the deployed image with that one directory added
   (`docker/loadtest/Dockerfile`, `FROM` the deployed one), serving `loadtest.standins.app:app` —
   the deployed command word for word, but for that;
3. that module refuses to load against a database or a Redis that is not on this machine;
4. the stack is given placeholder keys, and is never shown the repository's `.env`.

A test holds each of the four (`tests/unit/test_phase30_standins.py`).

**Each stand-in takes as long as the real thing.** A fake that answers at once moves the bottleneck
to wherever the fakes are not. The waits are drawn between the fastest and the slowest each was seen
to take, in Phase 29's traces and in one more plan made on real providers for this phase:

| | Real: Phase 29 | Real: this phase | Stand-in: fastest · usual · slowest |
|---|---|---|---|
| Intent parsing (Gemini) | about 2.4 s | 4.6 s | 1.8 · 2.4 · 4.6 s |
| Local tips (Groq) | 1.3 s | 2.5 s | 1.0 · 1.4 · 2.6 s |
| The itinerary (Groq) | 8.4 s | 5.3 s, the last 1.8 s arriving in 35 pieces | 5.0 · 6.0 · 8.4 s, written out at the same 1,460 characters a second |
| Preferences (Groq) | 1.0 s | 0.9 s | 0.7 · 0.95 · 1.5 s |
| An embedding (Gemini) | — | 3.3 s and 0.9 s | 0.8 · 1.0 · 3.3 s |
| Flights (Duffel) | 6.9 s | 3.4 s | 3.4 · 5.5 · 7.0 s |
| Hotels (LiteAPI) | 7.4 s | 5.7 s | 5.5 · 6.5 · 7.5 s |
| Geocoding (Nominatim) | 2.1 s | 2.2 s | 1.0 · 2.1 · 2.2 s |
| Attractions (OpenTripMap) | 2.1, 0.7, 0.7 s | 1.2, 0.8, 0.8 s | 0.7 · 0.8 · 2.1 s |

**The sanity check: one real plan, and the same plan by the stand-ins.** Jaipur, four days, two
travellers, on the production-like stack with real keys, then on the stand-ins:

| | Real providers and models | Stand-ins |
|---|---|---|
| The plan took | 12.6 s | 11.5 s (10 to 16 s across the runs below) |
| Events on the stream | `planning_started`, three searches, 35 pieces, `planning_complete` | the same, 28 to 39 pieces |
| Steps logged | 12, in this order: intent parsing … preference extractor, orchestrator | the same 12, in the same order |
| The trace of `POST /plan` | 105 spans: 65 SQL statements, 16 Redis commands, 24 of the app's own steps; 6 of the 105 in the tool server | 95 spans: the same 16, the same 24, the same 6 in the tool server — and 55 SQL statements: ten fewer, the ones section 9 removed |

What the stand-ins leave out, and so what these figures do not include: the processor time of
talking to a provider (TLS, an HTTP client, a JSON document per token); a Gemini that does not answer
for 15 seconds (seen once in Phase 29); a provider's own "too many requests". The app's own limit on
flight searches — the provider's 30 a minute — is real and was kept out of the way on purpose: the
load asks for 24 different flight searches, and every other is a cache hit.

## 3. What was measured, and how

**A visit** (`loadtest/k6/traveller.js`) is what the site does for someone who comes to plan a trip:
sign in (half the visits — a login lasts a day), the trips list, create a trip, the trip's page
(`GET /trips/{id}`, `/status`) and its event stream opened with `?token=` as a browser must, a few
seconds of the traveller writing, `POST /plan`, `/status` every 3 s while the plan is made, then
`/itinerary`, `/trips/{id}`, `/status`, `/similar` — and the page left open for 10 to 30 seconds
while it is read. The roadmap's four endpoints and the six the page asks around them. Not in it:
refinements, the PDF, search.

**Visits start at a fixed rate, whatever the server does.** A fixed number of virtual users each
waiting for its answer slows down with the server and so reports a slow server as a quiet one. Each
rate is kept up for 180 seconds, on a stack rebuilt from an empty database before each ladder.

**How long a plan takes is its own measurement**: from `POST /plan` — which answers 202 in
milliseconds and says nothing — to `planning_complete` on the stream.

**A rate holds** when at least 99 % of the visits get their plan, a plan takes under 30 s at p95,
requests that are not streams take under 1 s at p99, and fewer than 1 % of them fail. Decided before
anything was measured.

**k6 needed building.** It has no event streams: `http.get` on one waits for a response that never
ends and reports a timeout. The `xk6-sse` extension reads them; k6's build service would not build
it ("unknown dependency"), so k6 v2.3.0 is built with it by the `grafana/xk6` image. Three things
about it are in the script because they were found by trying: no timer fires while a stream is
open, so `/status` is asked in bursts when an event arrives; an event's name keeps the `\r` of this
server's line endings; and a stream's whole life is reported as an HTTP request's duration — the
latency figures here are of the requests that are not streams.

**While k6 loads the stack, the runner looks at it** every two seconds: the pooler (`SHOW POOLS`:
who is being served, who is waiting), Postgres (`pg_stat_activity`), the backend's own `/metrics`,
and Docker's memory. Three numbers were added to the app for this before the baseline, and stay:
`db_pool_connections`, `redis_pool_connections` (each with its limit) and `event_loop_lag_seconds`.
They change nothing about what the app can do.

**One request in ten was traced.** The default is every request. On one machine the trace store is
part of the machine, so the cost of tracing everything was measured — the same 1.6 visits a second,
both ways ([loadtest/traced-everything/](loadtest/traced-everything/)):

| At 1.6 visits a second | 1 request in 10 traced | Every request traced |
|---|---|---|
| Backend processor | 0.33 cores | 0.35 cores |
| Request p50 / p95 / p99 | 7 / 100 / 198 ms | 7 / 102 / 194 ms |
| A plan, p50 / p95 | 11.9 / 14.1 s | 12.0 / 14.0 s |
| Jaeger's memory at the end of the step | 89 MB | 288 MB |

The app hardly notices: two hundredths of a core. The trace store does — 200 MB in three minutes,
kept in memory, inside the same 8 GB the database has — and a forty-minute ladder would have had
the observer competing with the observed. So: one in ten, the same before and after. A deployment's
trace backend is somewhere else, and this says nothing about what it should sample.

**The machine had to stay awake.** The first smoke run had a 34-second hole in it: the laptop's lid
was closed, on battery, and macOS was asleep four seconds in every nine while everything appeared to
run. The runner now compares the wall clock with the clock programs time by, across every run, and
does not keep a run that slept. None of the runs below did.

## 4. Written before the baseline: what one plan shows, and what is expected

This section was written on 2026-10-12, after one plan had been watched and before any load had been
put on the stack. It is kept as it was written; what the baseline then showed is in section 5.

### What one plan does to the database

One plan was made on the production-like stack as Phase 29 left it — real models, real providers —
while Postgres was asked every half second what its connections were doing
([loadtest/real-plan-jaipur.json](loadtest/real-plan-jaipur.json)):

```
seconds after POST /plan    Postgres (pg_stat_activity)
 0.0 –  4.2                 idle                              intent parsing: the model is being asked
 4.8 – 12.5                 idle in transaction ×1            every later step, including 2.5 s and 5.3 s of waiting for a model
12.6                        planning_complete
13.1                        idle in transaction ×2            the run, and the job that embeds the new itinerary
13.7 – 16.2 (and on)        idle in transaction ×1            the embedding job: two calls to the embedding API, 3.3 s and 0.9 s
```

From its first logged step to its last, a planning run keeps one connection **inside an open
transaction** — for about 8 of this plan's 12.6 seconds, almost all of it spent waiting for a model.
The embedding job that follows keeps another for 4 more. The same plan made by the stand-ins shows
the same thing ([loadtest/standin-plan-jaipur.json](loadtest/standin-plan-jaipur.json): idle in
transaction from 2.5 s to 14.2 s).

Why, from the code: every step of a run ends in `log_agent_run`, which commits its row and then
refreshes it. The refresh is a `SELECT`, which begins a new transaction, and nothing ends that
transaction until the next step commits — seconds later, after the model has answered.

### What that predicts

The stack's connections, from the app to the database:

| Where | How many | What happens past it |
|---|---|---|
| The app's SQLAlchemy pool | 5, and 10 more as overflow: **15** (the library's defaults; the app sets neither) | a request waits up to 30 s for a connection, then fails |
| The pooler (PgBouncer, transaction mode) | **4** server connections (`default_pool_size = 4` in `docker/prodlike/pgbouncer.ini`) | the statement waits in the pooler's queue |
| Postgres | 100 | — |
| The app's Redis pool | **100** (the library's default; the app sets none). Every open event stream keeps one | the next command fails at once: "Too many connections" |

A transaction-mode pooler lends a server connection for one transaction. A connection that is idle
*inside* a transaction is still lent.

1. **The guess this phase started with** (Shauryaman's, recorded as a guess): *the pool runs out at
   around 15 concurrent plans* — the app's own pool.
2. **What the one plan suggests instead**: the pooler runs out first, at about **4 plans in
   flight**, because each holds one of its four server connections for most of its length. Past that,
   every statement — a plan's next step, a `GET /status` — waits at the pooler for some other plan's
   model call to finish. At 12 s a plan, 4 at once is about one plan every 3 seconds: expected to cope
   at 0.1 visits a second, to be visibly slower by 0.25–0.4, and by 0.8 to have plans taking
   several times as long and requests taking seconds. The app's pool of 15 would be the second wall,
   reached when more than 15 of the app's sessions are waiting at the pooler: 30-second waits, then
   failures.
3. **Redis**: about 100 open trip pages is the end, whatever else is true — at 45 s a visit that is a
   little over 2 visits a second, and sooner if plans are slow, because a page stays open until its
   plan arrives.
4. **Signing in**: a password check takes the server about 190 ms during which it does nothing
   else. With half the visits signing in, every other request should now and then be late by that much.

Which of these is right is what the baseline is for.

## 5. The baseline — before any tuning

The stack as Phase 29 left it (commit `33e3f94`), with the stand-ins in and three gauges added
(section 3). 2026-10-13, 00:30–00:48 IST. Visits started at a fixed rate for 180 seconds, one rate
after another; the files are in [loadtest/baseline/](loadtest/baseline/).

| Visits a second | Plans made | Plans a second | A plan takes, p50 / p95 | Requests a second | Request p50 / p95 / p99 | Failed | Holds |
|---|---|---|---|---|---|---|---|
| 0.1 | 19 of 19 | 0.11 | 11.3 s / 13.2 s | 1.3 | 9 ms / 21 ms / 210 ms | 0.0% | yes |
| 0.2 | 37 of 37 | 0.21 | 11.4 s / 12.3 s | 2.6 | 9 ms / 697 ms / 1,296 ms | 0.0% | no |
| 0.4 | 72 of 73 | 0.40 | 25.2 s / 53.7 s | 7.2 | 1,194 ms / 4,047 ms / 6,430 ms | 0.0% | no |
| 0.8 | 51 of 145 | 0.28 | 127.1 s / 230.0 s | 42.2 | 351 ms / 11,731 ms / 15,810 ms | 0.0% | no |

| Visits a second | Plans at once, max | Streams open, max | Database pool in use, max | Pooler: served / waiting, max | Longest wait at the pooler | Idle in transaction, max | Event loop late, p99 | Backend CPU |
|---|---|---|---|---|---|---|---|---|
| 0.1 | 2 | 5 | 5 of 15 | 6 / 0 | 0 s | 3 | 10 ms | 0.05 cores |
| 0.2 | 4 | 10 | 7 of 15 | 8 / 3 | 1 s | 4 | 10 ms | 0.08 cores |
| 0.4 | 16 | 27 | 15 of 15 | 9 / 11 | 8 s | 4 | 250 ms | 0.13 cores |
| 0.8 | 43 | 88 | 15 of 15 | 7 / 11 | 6 s | 4 | 250 ms | 0.16 cores |

**The app as shipped copes with one visit every ten seconds, and not with one every five.** At 0.2 a
second plans still arrive on time, but one request in twenty takes over half a second. At 0.4 a plan
takes twice as long and every request takes a second. At 0.8 two travellers in three never get a plan:
55 gave up after three minutes of waiting, 39 could not even start. Nothing *failed* — not one
request was answered with an error, and the server finished all 106 plans it was asked for, the
last of them five minutes late. It was only ever waiting.

Not for the processor: the backend never used a sixth of one core. Not for the event loop either: it
ran what was due within 10 ms, and within 250 ms at worst — which is the password check of a sign-in
(prediction 4: `POST /auth/login` takes 210 ms, and at 0.1 a second that is the whole of the p99).

## 6. What the bottleneck is, and how that is known

**Database connections — held by plans that are not using them.**

1. **The pooler's four server connections are all in use from 0.2 visits a second on** (Postgres never
   saw more than 4 connections from it), **and on average 3.4 of the 4 were idle inside a
   transaction** at 0.4 and at 0.8. Clients waited at the pooler: 3 at 0.2, 11 at 0.4 — for up to 8
   seconds.
2. **The app's own pool filled next**: 15 of 15 in use at 0.4 and 0.8. And 15 − 4 = 11: the eleven
   waiting at the pooler are the rest of the app's pool. Past that a request waits inside the app for
   one of the fifteen.
3. **Where a slow plan's time went**, from the 17 plans traced during the two overloaded steps
   ([loadtest/baseline-traces.json](loadtest/baseline-traces.json)):

   | The run took | …asking models | …getting a database connection (15 or 16 times a plan) | …in its 42 statements |
   |---|---|---|---|
   | 12.6 s | 12.2 s | 0.1 s | 0.6 s |
   | 25.4 s | 15.4 s | 6.3 s | 6.6 s |
   | 42.6 s | 12.3 s | 24.2 s | 9.1 s |
   | 69.8 s | 13.3 s | 56.5 s | 4.0 s |
   | 132.5 s | 15.3 s | 122.8 s | 8.0 s |

   The models took the same 12 to 15 seconds in every one of them. Everything else is a plan
   waiting, between one step and the next, to be given a connection — and the connections it is
   waiting for are held by other plans that are waiting for a model.

That is the mechanism section 4 found in one plan, at scale: every step commits its row and then
reads it back, which opens a transaction that stays open until the next step — through the model
call in between. A plan holds a connection for about 10 of its 12 seconds, and the job that embeds
its itinerary for 3 or 4 more: some **14 connection-seconds a plan**.

### The first hypothesis was wrong about where

*The pool runs out at around 15 concurrent plans.* The pool of 15 does run out — but at 0.4 visits a
second, where a healthy system would have 5 plans in flight, not 15. The wall is hit at **4**, at the
pooler, and the 16 plans in flight that then fill the pool are there *because* they are already
queueing. Right about the kind of limit; wrong about which pool, and by a factor of four about when.

### The hypothesis the change is made on

Written 2026-10-13 00:51 IST, before any setting or code was changed:

> **The number of plans this stack can make at once is the number of database connections it can
> hold open — because every plan in flight holds one, idle, inside a transaction. With 4 connections
> at the pooler and 14 connection-seconds a plan, that is 4 ÷ 14 ≈ 0.3 plans a second**, which is
> where the baseline stopped coping (fine at 0.2, queueing at 0.4).

If that is right, changing nothing but the number of connections moves the wall by the same factor:

- **The change:** the pooler lends 90 server connections instead of 4, and the app's pool may open
  40 instead of 15.
- **Predicted:** 40 ÷ 14 ≈ 2.9 plans a second. The rates that failed — 0.2, 0.4, 0.8 — hold, with
  plans back at 11 to 13 seconds and requests back in milliseconds; so does 1.6 (about 20 plans in
  flight, 28 connections). 3.2 does not: it needs 45 connections of 40, and 144 pages open at once
  is past the Redis pool's 100.
- **And if it is wrong:** more connections change nothing, and the wait is somewhere this section
  did not look.

Why those two numbers, and not larger ones:

- **90 at the pooler** is what the pooler this one stands in for lends. Neon's is PgBouncer in
  transaction mode with `default_pool_size` = 90 % of the database's `max_connections`, and its
  smallest compute has 104: 94 per user and database
  ([Neon: connection pooling](https://neon.com/docs/connect/connection-pooling)). This stack's
  Postgres has 100. The 4 was Phase 27's, chosen for a stack that planned one trip at a time — it
  is why the wall came so early *here*; on the hosted pooler the app's own 15 would have been first.
- **40 in the app** (10 kept, 30 more on demand) because two instances are alive at once during a
  deploy (`overlapSeconds` in `railway.json`), and both pools have to fit in the pooler's 90.

## 7. After the configuration change

Only the two numbers of section 6 were changed (commit `33e3f94` plus the working tree of that
moment; the stack rebuilt from an empty database, as for the baseline). The same visits, the same
180 seconds a rate, 2026-10-13 00:54–01:21 IST; [loadtest/more-connections/](loadtest/more-connections/).

| Visits a second | Plans made | Plans a second | A plan takes, p50 / p95 | Requests a second | Request p50 / p95 / p99 | Failed | Holds |
|---|---|---|---|---|---|---|---|
| 0.1 | 18 of 18 | 0.10 | 12.1 s / 13.6 s | 1.3 | 8 ms / 23 ms / 211 ms | 0.0% | yes |
| 0.2 | 36 of 36 | 0.20 | 12.0 s / 13.9 s | 2.6 | 8 ms / 23 ms / 210 ms | 0.0% | yes |
| 0.4 | 72 of 72 | 0.40 | 11.7 s / 13.8 s | 5.2 | 7 ms / 26 ms / 210 ms | 0.0% | yes |
| 0.8 | 145 of 145 | 0.81 | 11.6 s / 13.5 s | 10.4 | 7 ms / 85 ms / 208 ms | 0.0% | yes |
| 1.6 | 289 of 289 | 1.61 | 11.9 s / 14.0 s | 20.8 | 7 ms / 74 ms / 199 ms | 0.0% | yes |
| 3.2 | 75 of 576 | 0.42 | 14.9 s / 17.0 s | 21.7 | 8 ms / 424 ms / 739 ms | 3.5% | no |

| Visits a second | Plans at once, max | Streams open, max | Database pool in use, max | Pooler: served / waiting, max | Longest wait at the pooler | Idle in transaction, max | Event loop late, p99 | Backend CPU |
|---|---|---|---|---|---|---|---|---|
| 0.1 | 2 | 5 | 3 of 40 | 6 / 0 | 0 s | 3 | 10 ms | 0.04 cores |
| 0.2 | 4 | 10 | 5 of 40 | 7 / 0 | 0 s | 4 | 10 ms | 0.08 cores |
| 0.4 | 7 | 18 | 9 of 40 | 11 / 0 | 0 s | 8 | 250 ms | 0.13 cores |
| 0.8 | 14 | 35 | 15 of 40 | 20 / 0 | 0 s | 15 | 250 ms | 0.21 cores |
| 1.6 | 26 | 67 | 30 of 40 | 32 / 0 | 0 s | 30 | 250 ms | 0.34 cores |
| 3.2 | 51 | 101 | 40 of 40 | 40 / 0 | 0 s | 40 | 250 ms | 0.27 cores |

**The hypothesis held, in each of the things it predicted.** The three rates that had failed hold,
with plans at 12 seconds and requests at the 210 ms of a sign-in; so does 1.6. Nothing waited at the
pooler at any rate. And the connections in use follow the plans in flight as closely as the
hypothesis says they must: 14 plans, 15 connections; 26 plans, 30 — every one of them idle inside a
transaction.

**Before: the most it held was 0.1 visits a second — 1.3 requests a second at 210 ms p99, and at 0.8
visits a second two travellers in three got no plan. After: 1.6 visits a second — 20.8 requests a
second at 199 ms p99, every plan made in under 14 seconds.** Sixteen times the visits, for two
numbers in two files.

It also stopped where the prediction said it would, and for the reason given second: at 3.2 visits
a second 101 pages were open, the Redis pool was at 100 of 100, and from the 31st second on the log
has one error, 4,900 times: `redis.exceptions.MaxConnectionsError: Too many connections`. 137
requests for a plan were answered 500 (all 137 arrived at the Sentry stand-in). The server went on
to finish 173 plans, and 98 of them could not say so — `SSE publish failed for planning_complete` —
so their travellers waited three minutes and gave up on plans that were sitting in the database.

## 8. Two things the configuration change did not do

Written 2026-10-13 01:22 IST, before either was touched.

### It moved the wall; it did not take the cause away

Every plan in flight still holds a connection it is not using: 30 of the 40 at 1.6 visits a second.
The capacity that was bought is `connections ÷ 14`, and connections are the one thing a hosted
Postgres has few of: Neon's smallest lends 94 for the whole database, to every instance together.
The cause is in the code — a transaction left open across a model call (section 4).

> **Hypothesis: a plan holds a connection only because a transaction is left open between its
> steps. If none is — if every step's transaction ends before the next wait begins — connections in
> use stop following plans in flight.**

- **The change:** `log_agent_run` no longer reads a row back after committing it; the two places
  that read and then wait for a model, and the embedding job, give the connection back before they
  wait.
- **Predicted:** at 1.6 visits a second the pool's in-use falls from 30 to under 10, and "idle in
  transaction" from 30 to at most one or two at any look. And the test that matters: **with the
  baseline's own connections put back — 4 at the pooler, 15 in the app — the rates that broke the
  baseline (0.4 and 0.8) hold, and so does 1.6**.
- **If it is wrong:** the small pools fill as before, and something else is holding them.

### The second wall is Redis, at a hundred open pages

> **Hypothesis: the app's Redis client opens at most 100 connections — the library's default, which
> nobody chose — and every open trip page keeps one for as long as it is open. At about 100 open
> pages every command that needs a connection fails at once.** (Prediction 3 of section 4, seen at
> 3.2 visits a second.)

- **The change:** `REDIS_MAX_CONNECTIONS`, a setting; 500 on the production-like stack.
- **Predicted:** 3.2 visits a second — about 150 pages open — holds, with no `MaxConnectionsError`
  in the log.
- **If it is wrong:** 3.2 fails the same way, or on something behind it.

## 9. After the code change — on the baseline's own connections

The question of section 8, asked the hard way: the code changed, and the connections put *back* to
what the baseline had — 4 at the pooler, 15 in the app (`python scripts/loadtest.py up --small-pools`).
Three rates: the two at which the baseline's plans slowed and then stopped arriving, and twice
the higher of them; 2026-10-13 01:32–01:45 IST;
[loadtest/fix-on-baseline-pools/](loadtest/fix-on-baseline-pools/).
(The Redis setting was already at its new 500 for these runs; at most 67 pages were open, so it
plays no part in them.)

| Visits a second | Plans made | Plans a second | A plan takes, p50 / p95 | Requests a second | Request p50 / p95 / p99 | Failed | Holds |
|---|---|---|---|---|---|---|---|
| 0.4 | 72 of 72 | 0.40 | 11.6 s / 13.5 s | 5.1 | 8 ms / 22 ms / 210 ms | 0.0% | yes |
| 0.8 | 145 of 145 | 0.81 | 11.7 s / 13.8 s | 10.4 | 7 ms / 53 ms / 202 ms | 0.0% | yes |
| 1.6 | 289 of 289 | 1.61 | 11.8 s / 13.5 s | 20.8 | 7 ms / 145 ms / 199 ms | 0.0% | yes |

| Visits a second | Plans at once, max | Streams open, max | Database pool in use, max | Pooler: served / waiting, max | Longest wait at the pooler | Idle in transaction, max | Event loop late, p99 | Backend CPU |
|---|---|---|---|---|---|---|---|---|
| 0.4 | 7 | 19 | 3 of 15 | 5 / 0 | 0 s | 4 | 250 ms | 0.12 cores |
| 0.8 | 14 | 34 | 2 of 15 | 5 / 0 | 0 s | 1 | 250 ms | 0.21 cores |
| 1.6 | 25 | 67 | 6 of 15 | 7 / 0 | 0 s | 4 | 250 ms | 0.35 cores |

**The hypothesis held.** The same four connections that could not carry one plan every two and a half
seconds carry four times that, with nobody waiting. What it predicted, against what happened at 1.6
visits a second:

| At 1.6 visits a second, about 25 plans in flight | More connections, old code | Old connections, new code |
|---|---|---|
| Database pool in use: on average / at most | 18.0 / 30 of 40 | 0.6 / 6 of 15 |
| Connections idle inside a transaction, on average | 17.8 | 0.4 |
| Server connections the pooler had out, at most | 29 of 90 | 3 of 4 |

One plan, watched as in section 4, now reads: no connection idle inside a transaction at any look
([loadtest/standin-plan-jaipur-after.json](loadtest/standin-plan-jaipur-after.json); before:
[from 2.5 s to 14.2 s](loadtest/standin-plan-jaipur.json)). And the plan section 4 began with —
Jaipur, on real providers and real models, made again on the tree as committed
([loadtest/real-plan-jaipur-after.json](loadtest/real-plan-jaipur-after.json), 8.5 s): of 23 looks
half a second apart, one caught a transaction in passing, 0.09 s after `POST /plan` — the request
itself — and none after it. Before, a connection sat idle inside one from 4.8 s on.

**What was changed in the code**, all of it one rule — *a read's transaction is ended before
something outside is waited for*:

- `log_agent_run` (every step of a run ends in it) commits its row and no longer reads it back:
  every column of the row is set in the code. And where it does go on to read — after a step that
  asked a model it adds up what the trip has cost, for Phase 26's alert — it ends that read's
  transaction before it returns.
- The two places that look something up and then ask a model — the preference extractor after a
  plan, and `POST /refine` reading a change request — and the search that embeds what was typed,
  hand the connection back first (`hand_back`).
- The embedding job asks the embedding API for both vectors *before* it touches the database again,
  and then writes everything in one transaction. It used to hold a connection through both calls —
  and through every retry of a 429.

A test asks it of a whole plan on a real database: at every wait — three models, the extractor's,
three tools, the embedding API — the session of the code that is waiting must not be in a transaction
(`tests/integration/test_phase30_connections_integration.py`).

### Which read it was — found by putting them back

Section 4 blamed the read-back, from reading the code. That was half of it, and the smaller half.
It was found when the fix was broken on purpose to see the load test fail — on a copy of the
repository, with the baseline's 4 + 15 connections, at 0.8 visits a second (2026-10-13 02:51–03:03
IST; [loadtest/read-back-restored/](loadtest/read-back-restored/),
[loadtest/log-agent-run-as-it-was/](loadtest/log-agent-run-as-it-was/)):

| `log_agent_run` | Plans made | A plan takes, p50 / p95 | Request p99 | Pool in use, mean / max | Waiting at the pooler, mean / max | Holds |
|---|---|---|---|---|---|---|
| as shipped (the table above) | 145 of 145 | 11.7 s / 13.8 s | 202 ms | 0.2 / 2 of 15 | 0 / 0 | yes |
| with the read-back put back, and nothing else | 145 of 145 | 11.7 s / 13.7 s | 209 ms | 1.5 / 10 of 15 | 0.1 / 5 | yes |
| as it was before this phase: the read-back, and the cost sums' transaction left open | 118 of 145 | 58.5 s / 163.6 s | 15,395 ms | 12.4 / 15 of 15 | 8.9 / 11 | no |

**The first of these was expected to fail, and held.** The read-back alone costs a plan about a
second and a half of a connection, not ten: the transaction it opens after a search is logged stays
open while the local tips are asked for, and is then ended — by the hand-back after the cost sums
of that step. What kept a connection through the long waits was the *other* read. A plan's steps
are logged in this order: intent parsing, preferences, flights, the budget, hotels, activities,
local tips, the itinerary, the evaluator, the save, the preference extractor. The step logged before
the itinerary's six seconds is the local tips — a step that asked a model, so the trip's cost is
added up, and that read's transaction was the one left open while the itinerary was written.

One function, two reads after a commit; with the function as it was and everything else as shipped,
the baseline is back — the pool at 15 of 15, eleven waiting at the pooler, the processor at 0.19 of
a core. Taking out the read-back alone would have left most of it in place.

## 10. After — the same ladder, on what is shipped

The code change, the 40 + 90 connections of section 6 (two instances must fit in the pooler during a
deploy, whatever the code does with them), and `REDIS_MAX_CONNECTIONS=500`. The stack rebuilt from an
empty database; the baseline's rates and three more; 2026-10-13 01:48–02:22 IST;
[loadtest/after/](loadtest/after/).

| Visits a second | Plans made | Plans a second | A plan takes, p50 / p95 | Requests a second | Request p50 / p95 / p99 | Failed | Holds |
|---|---|---|---|---|---|---|---|
| 0.1 | 19 of 19 | 0.11 | 11.1 s / 13.2 s | 1.3 | 9 ms / 20 ms / 211 ms | 0.0% | yes |
| 0.2 | 37 of 37 | 0.21 | 11.9 s / 14.9 s | 2.7 | 8 ms / 21 ms / 211 ms | 0.0% | yes |
| 0.4 | 73 of 73 | 0.41 | 11.9 s / 16.0 s | 5.2 | 8 ms / 24 ms / 209 ms | 0.0% | yes |
| 0.8 | 145 of 145 | 0.81 | 11.8 s / 13.6 s | 10.4 | 7 ms / 24 ms / 207 ms | 0.0% | yes |
| 1.6 | 289 of 289 | 1.61 | 11.9 s / 14.1 s | 20.8 | 7 ms / 100 ms / 198 ms | 0.0% | yes |
| 3.2 | 577 of 577 | 3.21 | 13.0 s / 15.4 s | 43.0 | 9 ms / 180 ms / 206 ms | 0.0% | yes |
| 6.4 | 187 of 1152 | 1.04 | 104.7 s / 327.5 s | 272.9 | 1,656 ms / 10,068 ms / 18,303 ms | 0.3% | no |

| Visits a second | Plans at once, max | Streams open, max | Database pool in use, max | Pooler: served / waiting, max | Longest wait at the pooler | Idle in transaction, max | Event loop late, p99 | Backend CPU |
|---|---|---|---|---|---|---|---|---|
| 0.1 | 2 | 5 | 2 of 40 | 4 / 0 | 0 s | 1 | 10 ms | 0.05 cores |
| 0.2 | 4 | 10 | 1 of 40 | 5 / 0 | 0 s | 1 | 25 ms | 0.08 cores |
| 0.4 | 8 | 19 | 2 of 40 | 6 / 0 | 0 s | 1 | 250 ms | 0.13 cores |
| 0.8 | 13 | 34 | 4 of 40 | 8 / 0 | 0 s | 2 | 250 ms | 0.21 cores |
| 1.6 | 24 | 70 | 5 of 40 | 9 / 0 | 0 s | 2 | 250 ms | 0.33 cores |
| 3.2 | 58 | 135 | 14 of 40 | 17 / 0 | 0 s | 6 | 250 ms | 0.61 cores |
| 6.4 | 438 | 537 | 40 of 40 | 40 / 0 | 0 s | 30 | 1000 ms | 0.93 cores |

**The Redis hypothesis held too:** 3.2 visits a second, the rate that ended in 4,900
`MaxConnectionsError`s, holds — 135 pages open at once on 135 of the 500 connections, 577 plans of
577, and of the backend's 15,845 log lines of those five minutes not one is an error.

**Before and after, rate for rate:**

| Visits a second | Before: plans made · a plan, p50 · request p99 | After: plans made · a plan, p50 · request p99 |
|---|---|---|
| 0.1 | 19 of 19 · 11.3 s · 210 ms | 19 of 19 · 11.1 s · 211 ms |
| 0.2 | 37 of 37 · 11.4 s · **1,296 ms** | 37 of 37 · 11.9 s · 211 ms |
| 0.4 | 72 of 73 · **25.2 s** · **6,430 ms** | 73 of 73 · 11.9 s · 209 ms |
| 0.8 | **51 of 145** · **127.1 s** · **15,810 ms** | 145 of 145 · 11.8 s · 207 ms |
| 1.6 | not tried: fewer than half got a plan at 0.8 | 289 of 289 · 11.9 s · 198 ms |
| 3.2 | — | 577 of 577 · 13.0 s · 206 ms |

![The dashboard while 1.6 visits a second were being served](loadtest/dashboard-under-load.png)

*Grafana two and a half minutes into the last run of the phase — the tree as committed, 1.6 visits
a second ([loadtest/final/](loadtest/final/): 289 plans of 289, a plan 11.7 s at p50 and 13.3 s at
p95, 20.5 requests a second at 7 / 45 / 197 ms). The row added for this phase, "Under load", is the
one above the log lines: 2 database connections in use of 40 with 24 plans being made, 65 Redis
connections for 65 open streams of 500, and the event loop a quarter of a second late one time in a
hundred — the password check.*

## 11. What limits it now

**One process, on one core.** At 6.4 visits a second the backend used 0.93 of a core, the event loop
ran what was due a second late, and everything behind it backed up: 438 plans in flight, 537 pages
open, both pools full — full of requests the process had not got round to, not of plans holding them.
The ladder says where the edge is: 0.61 of a core at 3.2 visits a second is about 0.19 core-seconds a
visit, so one process is out of processor at about **5 visits a second**.

**Half of that is passwords.** A sign-in checks a password with bcrypt, on the event loop: 190 ms
during which the process does nothing else. With half the visits signing in that is 95 ms of the
190 a visit costs. It is also the whole of the request p99 in every table above — 200 ms at every
rate that holds, because one request in a hundred arrives while a password is being checked. It was
not changed in this phase: it is a third finding, and it would have needed a third round of
measuring. Checked in a worker thread it would leave the loop free (bcrypt releases the interpreter
while it works), which should roughly double what one process carries and take the p99 down to the
tens of milliseconds.

**More worker processes are the roadmap's other candidate, and they are not free here.** Nothing in
the baseline called for them — the processor was the last thing to run out, not the first. When
they are added, four things in this app are per process and would be wrong in silence:

| Per process today | With several workers |
|---|---|
| `GET /metrics` and every gauge behind it | each scrape reaches one worker at random: counters jump about, the three gauges of section 3 show one worker's pool. Needs the client library's multi-process mode, or a port a worker |
| The tool server, and its count for each provider's rate limit | one tool server a worker, each allowing itself the provider's whole limit: Duffel's 30 a minute becomes 30 a worker. The count has to move to Redis (`rate_limiter.py` says so) |
| The database and Redis pools | every worker has its own: `DB_POOL_SIZE + DB_MAX_OVERFLOW`, times workers, times two during a deploy, must fit in the pooler |
| The set of planning runs waited for at shutdown | already right: each worker waits for its own |

**A page costs a Redis connection.** 500 is a larger number than 100, not a different design: at
6.4 visits a second, with 537 pages open, the pool was at 500 of 500 and the log said "Too many
connections" more than 23,000 times — the same end the 101st page had met. One subscription
for the whole process, handing each page its own trip's events, would make it a connection a process
and not a connection a page. Phase 31 kills Redis under open streams on purpose; that is the place
to change how they are held.

**The pool is larger than the code now needs.** At 3.2 visits a second 14 of the 40 were in use at
most, 2 on average. The 40 stays because of what it has to fit into, not because of what it carries.

## 12. What these figures are not

- **Not a deployment's.** One laptop, on battery, ran k6, the app, the database, the pooler, Redis
  and five observability containers. Docker's memory was never short (2.6 GB of 8 at the worst, the
  overloaded 6.4 step). A deployment has a network between its parts and other people's hardware.
- **Not the real providers'.** A plan that needs a flight search nobody has made in the last five
  minutes costs a real request to a provider that allows 30 a minute; past that the app queues, and
  then tells the traveller the search is busy. The load here was shaped to stay under that —
  24 different flight searches, 99 % of lookups answered from the cache at the higher rates. More
  different trips than that is a limit of the provider's plan, and no setting here.
- **Not every kind of visit.** No refinements, no PDF, no search. A refinement is a second kind of
  planning run and was fixed by the same change, but it was not loaded.
- **Not long.** Three minutes a rate. Nothing here says what an hour does — to memory, to the
  database's size, to a cache whose entries expire.
- **Each rate once.** How far two runs of one rate differ can be read off the three set-ups that
  all carry 0.8 visits a second: a plan's p50 11.6, 11.7, 11.8 s; the request p99 208, 202, 207 ms;
  the p95 85, 53, 24 ms. Differences of that size between two columns mean nothing — and a p95
  that sits on the edge of a password check moves a great deal.

## 13. The runs

| Kept as | What it was | When (IST) |
|---|---|---|
| [loadtest/real-plan-jaipur.json](loadtest/real-plan-jaipur.json), [its trace](loadtest/real-plan-jaipur-trace.json) | one plan on real providers and models | 12 October, 20:03 |
| section 4 | what one plan shows, and what is expected | written 12 October, 21:29 |
| [loadtest/baseline/](loadtest/baseline/) | the stack as Phase 29 left it: 4 + 15 connections, Redis 100 | 13 October, 00:30–00:48 |
| [loadtest/baseline-traces.json](loadtest/baseline-traces.json) | where the time went in the 17 plans traced while it was overloaded | |
| section 6 | the hypothesis; then the two settings | written 00:51; changed 00:52 |
| [loadtest/more-connections/](loadtest/more-connections/) | the configuration change alone: 90 + 40 | 00:54–01:21 |
| section 8 | the two further hypotheses; then the code | written 01:22; changed 01:24–01:26 |
| [loadtest/fix-on-baseline-pools/](loadtest/fix-on-baseline-pools/) | the code change, on the baseline's 4 + 15 | 01:32–01:45 |
| [loadtest/after/](loadtest/after/) | everything: the code change, 90 + 40, Redis 500 | 01:48–02:22 |
| [loadtest/traced-everything/](loadtest/traced-everything/) | 1.6 visits a second with every request traced | 02:26–02:30 |
| [loadtest/real-plan-jaipur-after.json](loadtest/real-plan-jaipur-after.json) | the real plan again, on the tree as committed | 03:06 |
| [loadtest/final/](loadtest/final/), [the dashboard during it](loadtest/dashboard-under-load.png) | the tree as committed, once more: 1.6 visits a second | 02:45–02:49 |
| [loadtest/read-back-restored/](loadtest/read-back-restored/), [loadtest/log-agent-run-as-it-was/](loadtest/log-agent-run-as-it-was/) | the fix broken on purpose, two ways, on the baseline's 4 + 15 (section 9) | 02:51–03:03 |
| [loadtest/standin-plan-jaipur.json](loadtest/standin-plan-jaipur.json), [after](loadtest/standin-plan-jaipur-after.json), [its trace](loadtest/standin-plan-jaipur-trace.json) | one stand-in plan before and after the code change | |

The times of the runs are the result files' own (`began`); the times of writing and changing are the
working session's record of them. Sections 4 to 8 stand as they were written, with one exception:
three clock times in them had been put down from memory and were minutes out, and were corrected
against that record when this report was finished. The order was as it reads — each hypothesis
before its change, each change before its run.

Each `rate-<r>.json` holds k6's figures, the stack's, a time series of both two seconds apart, and
the commit it ran on. To run any of it again: `HOW_TO_RUN.md`, Step 28.
