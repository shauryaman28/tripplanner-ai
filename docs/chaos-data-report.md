# Chaos report, the data layer — Phase 32

**Status: done, on one machine.** Exercises 5 and 6 of the roadmap: Postgres is killed while a plan is being
made — and at the exact point between the two writes that save one — and the backend is killed
between saving an itinerary and embedding it. Exercises 1 to 4 are [chaos-report.md](chaos-report.md).

> As there: **one laptop**, the production-like stack of Phase 27 — the image that is deployed —
> with stand-ins for the models and the providers. What is broken is real: `docker kill` of the
> Postgres container, and of the backend. There is no deployment, and none of this may be done to
> one.

## 1. Written before anything was broken

Written 2026-10-14 06:49 IST, from reading the code, before either exercise — or the code that runs
them — existed. Kept as written; what happened is in the sections after it.

The app reaches Postgres through a pooler (PgBouncer, transaction mode), as it will in production.
For these exercises the stack's Postgres is given a data directory that survives a restart — the
production-like stack's own is in memory, and a database that comes back empty would say nothing
about a failover.

### Exercise 5 — Postgres is killed while a plan is being made, and started again

*The roadmap asks:* does `pool_pre_ping` detect the dead connection and reconnect? *Acceptance:* the
app reconnects within 10 seconds.

What I expect, and why:

1. **While Postgres is away, every request that needs it is a 500** — "Something went wrong on our
   side" — because nothing names a database that cannot be reached (Redis got that in Phase 31;
   Postgres did not). `GET /ready` says 503. Nothing hangs: the pooler is still there, and says at
   once that it cannot reach its server.
2. **The plan in flight is lost, and its trip is left `planning` with nothing planning it** — the
   main thing I expect to have to fix. A run writes a row after every step and nothing catches a
   failure to: the run crashes at its next step. The crash is handled — the page is told, in the
   driver's words, not a traveller's — and then the handler marks the trip `failed`, *in the
   database*, which is not there. That raises out of the task. When Postgres is back the trip still
   says `planning`, `POST /plan` answers 409 "already in progress", and only restarting the backend
   (its start-up recovery) frees it.
3. **The app itself reconnects at once; the pooler may not.** The app's connections are to the
   pooler and never broke, and each is tried before it is used. But PgBouncer, by default, waits 15
   seconds (`server_login_retry`) after failing to log in to its server before it tries again, and
   refuses its clients meanwhile. So I expect the first good answer somewhere between at once and 15
   seconds after Postgres accepts connections — possibly outside the roadmap's 10, for the pooler's
   reason and not `pool_pre_ping`'s.
4. **Nothing committed is lost**, and nothing half-written is there: Postgres replays its log.

*What would show me wrong:* a request that hangs; an app that needs restarting before it can use
the database again; a row missing that had been committed.

### Exercise 5, the second part — killed between the two writes that save a plan

*The roadmap:* kill Postgres after the `itineraries` row is inserted but before `trips.status` is
updated; confirm full rollback by querying both tables.

A plan is saved in one transaction — the trip marked `completed`, the itinerary inserted, one
commit. I expect the two to be sent in the *other* order from the roadmap's: the ORM writes a
parent table before its child, so `UPDATE trips` and then `INSERT INTO itineraries`. The exercise
does not depend on my being right: it holds a lock that makes the transaction's second write wait,
reads from `pg_locks` that the first has been made, and kills Postgres there.

What I expect: **after the restart, neither.** No itinerary row for the trip, and `trips.status`
still `planning` — rolled back by Postgres itself; the app has no part in it. (And then the trip is
stuck as in point 2 above.)

*What would show me wrong:* an itinerary whose trip is not `completed`, or a `completed` trip with
no itinerary.

### Exercise 6 — the backend is killed after an itinerary is written and before it is embedded

*The roadmap asks:* is the embedding generated on the next start, or silently lost? *Acceptance:*
the queue drains after a restart; no embedding is lost for good; every `pending_retry` row is
retried at start-up.

What I expect, and why:

1. **Silently lost.** The itinerary is committed; then the embedding job is started as a task of
   the same process, and asks the embedding API for two vectors — seconds — before it writes
   anything. A backend killed in that window leaves an itinerary with *no row at all* in
   `embeddings`. Start-up recovery re-queues the itineraries that have a `pending_retry` row; this
   one has none. Nothing will ever embed it: it is missing from search and from "similar trips" for
   good, and nothing says so.
2. **A `pending_retry` row is retried.** With the embedding API failing, the job writes one such
   row; after a restart with the API answering I expect two vectors and the row gone — Phase 14's
   recovery, which I have no reason to doubt.
3. **The trip itself is whole:** `completed`, with its itinerary. What dies with the process is
   what comes after the save — the event that says the plan is ready, the preference extractor.

*What would show me wrong:* vectors for the first itinerary after the restart.

## 2. What happened — the app as Phase 31 left it

Run 2026-10-14 between 06:52 and 07:07 IST on the image built from Phase 31's tree, before any line
of `src/` was changed for this phase; written at 07:08 IST. Records: [chaos/before/](chaos/before/).

| Exercise | Expected (section 1) | What happened |
|---|---|---|
| 5 — Postgres away for 15 s | every request a 500; the plan lost and its trip stuck | **wrong on both counts.** All 8 requests sent meanwhile were answered `200` — late: the slowest waited 17.12 s. The plan in flight was made. A plan *asked for* while Postgres was away was accepted 13 s later, and made. The app answered 0.9 s after Postgres took connections again |
| 5 — away for 45 s | the same | 23 requests: 15 were a 500, the rest answered once Postgres was back — **the slowest after 60 s**. The plan in flight was made, 72 s after it was asked for. **The app could not use the database for 13.7 s after Postgres was accepting connections** |
| 5 — killed between the two writes | neither write survives; the trip stuck `planning` | **neither write survived** — no itinerary, the trip not `completed`: Postgres's doing. The trip was *not* stuck: it was marked `failed`. But the traveller's page was told why like this: `(sqlalchemy.dialects.postgresql.asyncpg.Error) <class 'asyncpg.exceptions.ConnectionDoesNotExistError'>: connection wa…` — and on, through the statement and its parameters |
| 6 — backend killed before the embedding | silently lost | **silently lost.** Killed 0.21 s after `planning_complete`: no row in `embeddings`; two minutes after the restart, still none; "similar trips" says `pending` and always would |
| 6 — restarted with an embedding owed | retried | **retried**: two vectors 4.2 s after the backend was ready, the row that owed them gone |

### Where section 1 was wrong

- **A pooler does not refuse a query while its database is away. It keeps it.** I expected errors at
  once. PgBouncer holds a query for which it has no server connection — for two minutes, by
  default — and hands it over when Postgres is back. So a short outage is not an error at all, to
  anything: requests wait and are answered, a plan in flight stops between two steps and goes on. I
  had predicted the plan lost and its trip stuck `planning`; with 15 seconds, and with 45, it was
  made. What the pooler's patience costs is that *nothing bounds the wait*: a request sat for
  60 seconds, and would have sat for 120.
- **The trip was not left `planning`.** The run's crash handler marks the trip in the database —
  and that statement, too, waited in the pooler until there was a database to run it. My reasoning
  (the database is not there, so it cannot be marked) held for as long as the pooler's patience
  lasts, which is longer than any of these outages.
- **The order of the two writes is the roadmap's, not mine.** The itinerary is inserted first, then
  the trip is updated. The first run of the exercise held the wrong lock, stopped the first write
  and so killed Postgres with *nothing* written — a rollback of nothing
  ([its record](chaos/before/postgres-kill-mid-save-first-try.json)). It was rebuilt to hold the
  trip's own row, in the mode an `UPDATE` needs and an insert pointing at it does not, and run again
  on the same unchanged app: `UPDATE trips SET status=$1::VARCHAR WH…` waiting, the
  itinerary written, and then the kill.
- **The 15 seconds were the pooler's, as predicted — but not always.** After a failed attempt to
  reach Postgres, PgBouncer waits `server_login_retry` (15 s) before the next and refuses new
  queries meanwhile: that is where the 500s came from, and the 13.7 seconds. When no attempt happened to
  fail just before Postgres returned, the app was back in under a second. The app's own part —
  `pool_pre_ping`, the roadmap's question — never came into it: its connections are to the pooler,
  and the pooler did not go anywhere.

What section 1 got right: the traveller is told in the driver's words; the embedding is lost; a row
that says an embedding is owed is retried; Postgres rolls back half a save by itself.

One thing nobody predicted, found by the counts that are taken after every exercise: a backend
killed just after it has saved a plan leaves a `completed` trip whose run has no closing row — the
steps after the save (the event that says the plan is ready, the preference extractor) died with
the process. The plan stands. It is recorded here and not changed.

## 3. What was changed

Less than section 1 expected to have to change, and not what it expected: behind the pooler the
short outage already went well. What was wrong was at its edges.

| Found | Changed | Held by |
|---|---|---|
| A traveller's page was shown what the driver raised — the statement a save had failed on, and the plan in its parameters | A run that ends for a reason not in the trip says one of three sentences: the planner lost its database for a moment; planning took too long; something went wrong and has been reported. What was raised is in the log and the error report | `test_a_save_that_loses_its_database_half_way_leaves_nothing_and_says_so_in_words` (real Postgres) |
| A request that met a database it could not reach was a 500, "something went wrong on our side" | "The database cannot be reached" is told from every other failure (`app/db/errors.py`) and answered 503 with `Retry-After`, in words. A statement the database *refused* is still a 500, and still reported | `test_a_request_that_finds_the_database_away_is_a_503_and_a_refused_statement_still_a_500` |
| The app could not use the database for 11.6 s and 13.7 s after Postgres was accepting connections: the pooler looks for its server again only every 15 s | `server_login_retry = 5` on the stand-in pooler. (What a hosted pooler waits is its operator's choice) | `test_the_pooler_looks_for_its_database_again_within_the_roadmaps_ten_seconds` |
| A crashed run marks its trip from the session whose connection has just died. Behind the pooler that statement waited and was run; past the pooler's patience, or without one, the trip would say `planning` with nothing planning it | Marked from a session of its own, again and again for up to ten minutes while the database is away, with the reason as the run's closing row. Only while the trip is as that run left it | `test_a_run_that_could_not_mark_its_trip_goes_on_trying_until_the_database_is_back` |
| An itinerary whose backend was killed before it was embedded had no row in `embeddings`, and start-up recovery looks for rows that say "owed" | The row that says "owed" is written in the transaction that saves the itinerary | `test_a_plan_is_saved_owing_its_embeddings_and_the_next_start_pays` (real Postgres) |
| Start-up recovery that found the database away was skipped until the next restart | It looks again every 15 s, for ten minutes | `test_a_backend_that_starts_while_its_database_is_away_looks_again` |

**Not changed, because nothing was wrong:** half a save. One transaction, rolled back by Postgres;
the exercise stays as the check that nobody splits that commit.

**Not changed, because nothing was asked of it:** `pool_pre_ping`. The app's connections are to the
pooler, which never went away. The roadmap's question is asked where it does arise — with no pooler
in between, every connection the pool holds ended at the server — in
`test_connections_killed_at_the_server_are_replaced_before_a_request_meets_one`: no request met one.

### Tried, and taken back out: the driver's own timeout

Nothing in the app bounds how long a pooler may keep a query: with Postgres away for 45 seconds a
request waited a minute, and would have waited two. So the driver was given `command_timeout = 20`,
as the app's own limit — and the same exercise was run again
([its record](chaos/tried/postgres-kill-45s-with-a-driver-timeout.json)):

```
sent  0.2 s after the kill  →  500  after 49.6 s
sent  2.2 s                 →  500  after 47.7 s
…                              (eight of them)
sent 14.2 s                 →  500  after 35.8 s
```

Not one failed after 20 seconds. All of them failed at the same instant, three seconds after
Postgres was back — with a bare `TimeoutError`, a 500 — and so did the plan in flight; before the
change every one of those requests had been *answered*, and the plan made. The driver's timeout
takes effect when the server answers the cancel request the driver sends it. A server that is away
answers nothing; once it is back it cancels the query it was about to run. It bounds nothing while
the database is gone, and turns late answers into errors when it returns.

It was removed. What bounds the wait is the pooler's own patience (`query_wait_timeout`, two
minutes by default, on this stack and on the hosted pooler it stands in for), and that is a limit
of this phase: section 5.

### And one mistake of my own

The first build with the "owed" row in it failed every plan. The itinerary and the row that points
at it were written in one flush; there is a foreign key between the two tables and no relationship
between the two classes, so nothing told the ORM which to send first, and it sent the embedding.
The unit test had passed — its database was a mock. The integration suite, which I had not run
before rebuilding the stack, fails on it at once; it is now run first. The itinerary is flushed
before the row is added, in the same transaction.

## 4. After

Every exercise on the tree as committed, 2026-10-14 07:20–07:24 IST, the machine awake for all
of it (`asleep_s: 0` in each record). Written at 07:25 IST. Records: [chaos/after/](chaos/after/).

| Exercise | Before | After |
|---|---|---|
| 5 — Postgres away 15 s | 8 requests, all answered, the slowest after 17 s; the plan in flight made; the app back 0.9 s after Postgres | the same: 8 requests, all `200`, the slowest after 17 s; the plan made 29 s after it was asked for; the app back 0.8 s after Postgres. A pause, before and after |
| 5 — away 45 s | 15 of 23 requests a 500, the rest answered after up to 60 s; **the app back 13.7 s after Postgres** | **no 500**: 23 of 23 told `503 … please try again shortly` within 0.1 s; the plan in flight still made, 61 s after it was asked for; **the app back 2.7 s after Postgres** |
| 5 — killed between the two writes | no itinerary, the trip not `completed`; the page shown the statement that failed | no itinerary, the trip not `completed` — and the page told "The plan could not be saved: the planner lost its database for a…", the same from `GET /status`; asked for again, one itinerary and the trip `completed` |
| 6 — backend killed before the embedding | no row; none two minutes after the restart: lost | killed 0.23 s after `planning_complete` with the itinerary on record as owed (`{'vectors': 0, 'pending_retry': 1}`); **two vectors 4.3 s after the backend was ready** |
| 6 — restarted with an embedding owed | retried | retried: two vectors 4.3 s after ready |

Two things about the 45 seconds are the pooler's and not the app's, and differ from run to run.
Whether a query is *kept* or *refused* while Postgres is away: before, eight were kept and answered
after up to a minute; after, all 23 were refused at once. The app now does the right thing with
either — answers late, or says "try again shortly" — where a refusal used to be a 500. And how soon
the pooler looks again: 13.7 s before, 2.7 s after, with its retry at 5 seconds where it was 15.

The counts of rows that must not exist were all zero after every exercise.

### Against the roadmap's acceptance

| | |
|---|---|
| Postgres kill → app reconnects within 10 s | 0.8 s and 2.7 s after Postgres accepted connections (1.6 s in the mid-save run); before, 13.7 s and 11.6 s |
| Does `pool_pre_ping` detect the dead connection and reconnect? | behind the pooler it is never asked; without one — every pooled connection ended at the server — no request met a dead connection |
| Half-committed scenario → full rollback, confirmed by querying both tables | `itineraries`: 0 rows for the trip; `trips.status`: `failed` |
| Embedding queue drains after restart; no permanently lost embeddings | `{'vectors': 2, 'pending_retry': 0}` for the itinerary whose backend was killed |
| All `pending_retry` rows retried on next startup | `{'vectors': 2, 'pending_retry': 0}` |

## 5. What this does not show

- **A deployment**, a hosted Postgres or its pooler. What the stand-in pooler does while its database
  is away — keep a query, refuse it, look again after 5 seconds — is PgBouncer as configured here.
- **A limit on how long a request waits.** There is none in the app (section 3): the pooler's two
  minutes are the limit, and a request can hang until the frontend's host gives up on it.
- **An outage longer than the pooler's patience**, and the stack without a pooler: not exercised on
  the stack. One integration test stands for the second.
- **An embedding owed is paid at the next start**, not before: a job that fails while its backend
  lives waits for a restart.
