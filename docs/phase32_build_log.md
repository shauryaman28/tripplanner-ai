# Phase 32 — Chaos Engineering: Data Layer

**Status: done, on one machine.** Postgres was killed while a plan was being made — for 15 seconds, for 45, and at the point between the two writes that save a plan — and the backend was killed between saving an itinerary and embedding it. Each was written down as a hypothesis first, run on the app as Phase 31 left it, fixed where it failed, and run again. On the production-like stack of Phase 27 with the stand-ins of Phase 30. **Not on a deployment: there is none, and none of this may be done to one.**

**What the phase delivers (roadmap):** Postgres failover scenario documented. Embedding queue drains correctly after recovery.

The report: [chaos-data-report.md](chaos-data-report.md). Decisions: `DECISIONS.md` #272–#283. How to run it: `HOW_TO_RUN.md`, Step 30.

Five things are worth knowing before reading on.

- **Behind a pooler, a short outage of the database is a pause.** I expected errors at once, a plan lost and its trip stuck. PgBouncer keeps a query it has no database for and hands it over when Postgres is back: every request was answered, late; the plan in flight stopped between two steps and went on. Three of my predictions were wrong about that, and are kept.
- **What was wrong was at the edges of the pause.** A traveller was shown the SQL a save had failed on. The app could not use the database for 11 to 14 seconds after it was back, because the pooler looks for it only every 15. And an itinerary whose backend died before embedding it was never embedded — the one thing predicted exactly.
- **Half a save is rolled back by Postgres**, as it should be — and the first version of the exercise that "showed" it stopped the wrong statement and rolled back nothing.
- **One fix was tried and taken back out.** The driver's own timeout does not bound a query that is waiting in a pooler: it turned nine answers into nine errors. How long a request can wait for a database that is away is the pooler's decision, and this phase leaves it there, said as a limit.
- **One of my fixes failed every plan on the first rebuild** — a unit test with a mock database had passed it. The integration suite is now run before a stack is rebuilt.

## What was built

```
src/backend/app/db/errors.py            ← NEW: "the database cannot be reached", told from a statement it refused
src/backend/app/main.py                 ← that is a 503 in words; start-up recovery that could not look, looks again
src/backend/app/api/routes/trips.py     ← a traveller is told what happened, never what was raised; a run that
                                          ended without marking its trip goes on trying to, from a session of its own
src/ai/orchestrator/orchestrator.py     ← an itinerary is saved together with the row that says its embeddings are owed
src/backend/app/db/session.py           ← no driver timeout — and why not
docker/prodlike/pgbouncer.ini           ← server_login_retry = 5 (was the default, 15)

scripts/chaos.py                        ← exercises 5 and 6: postgres-kill (15 s, 45 s, --mid-save), backend-kill (--pending)
docker/chaos/compose.yml                ← for chaos, Postgres keeps its data when it is killed
loadtest/standins/faults.py, models.py  ← the embedding API can be made to fail
docs/chaos-data-report.md               ← NEW: expected, what happened, what was changed, after
docs/chaos/before/, after/, tried/      ← every record
```

## The order things were done in

Times are from `date` in the command that wrote each section, and from the records' own `began`.

| When (IST, 14 October) | What |
|---|---|
| 06:49 | **The hypotheses written**, before either exercise or the code that runs them existed |
| 06:52–07:07 | **Run on the app as Phase 31 left it** (`docs/chaos/before/`). The mid-save exercise was found to stop the wrong statement, rebuilt, and run again on the same unchanged app |
| 07:08 | What happened written down; the fixes begun |
| 07:10 | The first rebuild with the fixes: every plan fails — my own mistake in one of them (#280). Stopped, fixed, the integration suite run |
| 07:12–07:17 | Run again: the 45-second outage is *worse* than before — the driver's timeout (#274). Taken back out |
| 07:20–07:24 | **Every exercise on the tree as committed** (`docs/chaos/after/`) |

## Dev A — the database

**Exercise 5: Postgres killed mid-run.** The roadmap asks whether `pool_pre_ping` detects the dead connection and reconnects, within 10 seconds. On the stack the question does not arise as asked: the app's connections are to the pooler, and the pooler does not go away. What happens instead is section 2 of the report — queries wait in the pooler and are answered when Postgres is back — and what was slow was the pooler's own look for its database (`server_login_retry`, 15 s by default; now 5). The app's part of reconnecting was a tenth of a second every time.

The roadmap's question is still answered where it arises: an integration test ends every connection the app's pool holds, at the server, and sends requests — none meets a dead one.

**The atomic transaction.** Killed with the itinerary inserted and the update of the trip waiting: after the restart, no itinerary and the trip not `completed`. Confirmed by querying both tables, as the roadmap asks. The order of the two writes is the roadmap's; I had it reversed, and the exercise's first version showed nothing because of it.

**Changed:** a database that is away is a 503 in words (#275); a traveller is told what happened, not what was raised (#276); a run that could not mark its trip goes on trying (#277); the pooler's retry (#278). **Tried and removed:** a driver timeout (#274).

## Dev B — the embedding queue

**Exercise 6: the backend killed between saving an itinerary and embedding it.** Killed 0.21 seconds after `planning_complete`. Before: no row in `embeddings`, none two minutes after the restart, "similar trips" `pending` for good — silently lost, as the roadmap feared. With an embedding already on record as owed, the restart paid it within five seconds: Phase 14's recovery works; what was missing was the record.

**Changed:** the row that says an itinerary's embeddings are owed is written in the transaction that saves the itinerary (#280) — so the queue the roadmap speaks of is a table, and an itinerary cannot be in the database without being in it. Start-up recovery that could not look, looks again (#281).

## Found and fixed along the way

**Every plan failed on the first rebuild.** The owed row was inserted before the itinerary it points at: a foreign key, no ORM relationship, one flush. A mock database had no opinion. Flushed first, in the same transaction; and the integration suite is run before a rebuild.

**The driver's timeout made the long outage worse** (#274): errors, all at one instant, where there had been answers.

**The mid-save exercise stopped the wrong statement.** It held a table lock that blocks inserts; the insert is the *first* write. It now holds the trip's row for the update, and reads from `pg_locks` that the itinerary has been written before it kills anything.

**One hanging request stretched a 15-second outage to 32.** The exercise sent its requests one after another and waited for each; the first one waited for Postgres, which was waiting for the exercise. Requests now go on threads of their own, and Postgres is started on the clock.

**A count that had never seen a killed backend.** "Trips whose rows do not end with the row that closes a run" found one after every `backend-kill`: the plan was saved, and the steps after the save died with the process. The plan stands; the count now allows for it (#282).

**The refinement fallback of Phase 15, nearly broken.** Marking a trip only while it still says `planning` would have left a refinement that made no plan as `failed` — an older test caught it before it left the tree. A trip is touched while it is as its run left it, which for that case is `failed`.

## Live results

Every exercise on the tree as committed, the machine awake (07:20–07:24 IST, 14 October): **5 of 5 as expected** — the table is the report's section 4, the records `docs/chaos/after/`. In short: Postgres away 15 s → every request answered once it was back, the plan in flight made, the app back 0.8 s after Postgres; away 45 s → no 500 (23 of 23 told to try again shortly, where 15 had been a 500), the plan still made, the app back 2.7 s after Postgres (13.7 before); killed between the two writes → no itinerary and the trip not `completed`, the traveller told "the planner lost its database for a moment", and asked for again one itinerary and the trip `completed`; the backend killed 0.23 s after `planning_complete` → the itinerary on record as owed, two vectors 4.3 s after the backend was ready; with an embedding owed from before → two vectors 4.3 s after ready.

## Tests

**1,290 unit + contract, 79 integration, 60 browser — all passing** on the tree as committed. New in this phase: 35 unit, 5 integration.

- **`tests/unit/test_phase32_data_layer.py` (35)** — "the database cannot be reached" for each way it shows (the driver's errors under SQLAlchemy's, a pool with no connection to give, a refused connection) and for each thing it is not (a constraint, a bad statement, a deadline elsewhere); the 503 and what stays a 500; that the driver is given no timeout; the three sentences a traveller is told and that none carries what was raised; a run that could not mark its trip trying until the database is back, giving up in the end, and not undoing what the traveller has started since; the owed row in the commit that saves the itinerary, the itinerary sent first; start-up recovery that looks again; the pooler's retry; Postgres keeping its data for chaos; the embedding fault; the mid-save exercise stopping the second write.
- **`tests/integration/test_phase32_data_integration.py` (5)** — on a real Postgres: every connection the pool holds ended at the server, and no request meets one (`pool_pre_ping`, the roadmap's question, without a pooler in the way); a request that finds the database away is a 503 and a refused statement still raised; a save that loses its connection half-way leaves nothing, tells the traveller in words, and is saved whole when asked for again; a plan saved owing its embeddings, paid by what a backend does as it starts; a plan embedded in the ordinary way owes nothing afterwards.
- Changed: `test_phase15_refinement.py` (a crashed run tells the traveller it went wrong, not what was raised), `test_phase20_streaming.py` (the shared fake database can flush), `test_phase31_harness.py` (the chaos overlay now has Postgres in it).

### Break it on purpose

Not run yet when this was first committed (the laptop at 9 % battery, as in Phase 31): see the end of this section for what came of it.

## Done criterion checklist

| | Criterion | |
|---|---|---|
| ✅ | Chaos exercise 5: kill the Postgres container mid-itinerary write | 15 s, 45 s, and between the two writes of the save |
| ✅ | Does `pool_pre_ping` detect the dead connection and reconnect? | behind the pooler it is never asked to; without one — every pooled connection ended at the server — no request meets a dead connection (integration test) |
| ✅ | Postgres kill → app reconnects within 10 s | see "Live results"; before: 11.6 s and 13.7 s, the pooler's doing |
| ✅ | Kill after the `itineraries` row inserts but before `trips.status` updates → full rollback, confirmed by querying both tables | no itinerary, the trip not `completed` |
| ✅ | Chaos exercise 6: kill the FastAPI process after an itinerary is written, before its embedding | 0.2 s after `planning_complete` |
| ✅ | Embedding queue drains after restart; no permanently lost embeddings | before: lost. Now the itinerary is saved owing them, and the restart pays |
| ✅ | All `pending_retry` rows retried on next startup, confirmed in the `embeddings` table | two vectors, nothing owed |
| ◻ | …on a deployment, against a hosted Postgres and its pooler | there is none |

✅ done and seen on the stack · ◻ not possible yet

## Known limits

- **How long a request waits for a database that is away is the pooler's decision** — two minutes by default, on this stack and on the hosted pooler. The app adds no limit of its own: the one tried (#274) did harm. A request can therefore hang until the frontend's host gives up on it.
- **An outage longer than the pooler's patience** was not exercised. Then queries fail, a run in flight crashes and marks its trip `failed` — from a session of its own, for up to ten minutes (#277) — and after that only a restart does.
- **Without a pooler** (the unpooled URL, a developer's machine) the stack was not exercised; one integration test stands for it.
- **An embedding that is owed waits for the next start** (#282). A job that fails while its backend lives is not tried again until then.
- **The stand-in pooler's numbers are the stand-in's.** What a hosted pooler waits before looking for its database again is not known to this repository.
- **The embedding fault is the stand-in failing**, not the provider's client (#283).
- One laptop; stand-ins; no deployment.
