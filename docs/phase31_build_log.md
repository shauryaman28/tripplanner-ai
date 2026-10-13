# Phase 31 — Chaos Engineering: Agent Failures

**Status: done, on one machine.** Four things the app depends on were broken on purpose while it planned a trip — the tool server killed, Gemini answering 429, the builder's model never answering, Redis killed under open event streams — on the production-like stack of Phase 27 with the stand-ins of Phase 30. Each was written down as a hypothesis first, run on the app as Phase 30 left it, fixed where it failed, and run again. **Not on a deployment: there is none, and none of this may be done to one.**

**What the phase delivers (roadmap):** Kill an agent mid-run — system degrades gracefully, recovers, no data corruption.

The report: [chaos-report.md](chaos-report.md). Decisions: `DECISIONS.md` #258–#271. How to run it: `HOW_TO_RUN.md`, Step 29.

Five things are worth knowing before reading on.

- **Three of the four exercises found something a traveller would have met.** A killed tool server took every later search with it until the backend was restarted. A model that never answered kept a trip "planning" for as long as the process lived. A plan in flight when Redis went away took as long as the outage did, and a plan asked for meanwhile was a bare 500. Gemini's 429 — the one the roadmap was least sure of — cost nothing.
- **Two of my four hypotheses were wrong, and both are kept.** I expected the silent model to fail a trip after about twelve minutes; it would never have. I expected a run under a dead Redis to be untouched; it was slowed to the length of the outage. A third exercise did not test what it was set up to and was rebuilt.
- **A model fault is the provider's own client pointed at a server that fails.** A stand-in that raised a timeout would have tested my idea of what the client does. The real one showed what no reading of the app's code had: with no timeout given, the client has none.
- **Killing the real process under the fix found two more bugs — in the fix.** Putting a dead tool server away from the task that found it cancelled whichever task had started it; and a call sent a moment after its pipe closed waited for ever. Neither shows against a mock.
- **Nothing in Postgres was left half-done by any exercise but one**: the silent model, before the fix, left its trip `planning` with nothing planning it.

## What was built

```
loadtest/standins/
  faults.py                             ← NEW: which model is to go wrong, and how — a file in the load test's container,
                                          read each time a model is made. With a fault on, that model is the
                                          provider's real client, with the app's own options
  fault_server.py                       ← NEW: the far end: 429 in Gemini's words or Groq's, 500, or a request
                                          that is read and never answered; GET /log says what each client asked
  models.py                             ← the stand-in factories hand out the real client when a fault says so
docker/chaos/compose.yml                ← NEW: one more container on the load test's stack, for that server
scripts/chaos.py                        ← NEW: the four exercises (seven variants), each saying what it expects
                                          and keeping what it saw; up / down
scripts/loadtest.py                     ← the stack's commands take one more file to lay over it
docs/chaos-report.md                    ← NEW: expected, what happened, what was changed, after
docs/chaos/before/, after/              ← NEW: every exercise's record

src/ai/mcp_client/client.py             ← a dead tool server is replaced and the call asked again, once; one task
                                          owns a server from start to end; its pipe is watched; CONNECTION_REFUSED
src/ai/builder/builder.py               ← the model is waited for 20 s a read, 30 s in all; LLM_TIMEOUT; `missing`
src/ai/agents/preference_extractor.py   ← its model call has a timeout
src/ai/orchestrator/orchestrator.py     ← a timed-out build is not built again; "nothing could be searched";
                                          a plan made without a search says so in what is saved
src/ai/utils/failures.py                ← the traveller's words for a search service that stopped
src/backend/app/api/routes/trips.py     ← a run does not wait on Redis, has a deadline as a whole, and is started
                                          whatever Redis does; a planned trip whose state is gone is re-planned
src/backend/app/main.py                 ← Redis away: 503 in words
src/telemetry/metrics.py                ← mcp_tool_server_starts_total
```

## The order things were done in

Times are from `date` in the command that wrote each section, and from the records' own `began`.

| When (IST, 13 October) | What |
|---|---|
| 07:39 | **The four hypotheses written** — from reading the code, before the script that runs the exercises had been run |
| 07:44–08:04 | **Every exercise run on the app as Phase 30 left it** (`docs/chaos/before/`); written up at 08:04 |
| 07:50– | The fixes written, while the later "before" runs ran — on the tree, not in what was running |
| 08:04 | The laptop's lid closed, on battery. A run of the fixed app made after that is not used: the machine slept in cycles for twelve hours |
| 20:37– | The lid open again: the stack rebuilt, **every exercise run again** (`docs/chaos/after/`) |

## Dev A — the tool server, and Gemini's 429

**Exercise 1: the tool server killed.** Expected by the roadmap: `ToolError(code="CONNECTION_REFUSED")`, the affected agent `failed`, a plan from partial results, labelled. Found: the three searches failed in milliseconds with the reason `MCP client exception: ` — nothing after the colon — the trip was failed for its *destination*, and no search worked again until the backend was restarted: the client kept its dead session for the life of the process.

Changed (#261–#264): a call that finds the tool server gone starts another and asks again, once; what it is told otherwise is `CONNECTION_REFUSED`, with words for the traveller; a trip none of whose searches could be *run* says so; and a plan made without a search carries `missing` in the itinerary that is saved.

The exercise now has three variants, because one kill no longer leaves anything broken: killed once (the plan is whole — the searches were asked of the new server); kept dead from before any search answered (three `failed` rows, each `CONNECTION_REFUSED`, the trip failed for the right reason); kept dead from the moment the flights are in (a plan from what was found, saying what it lacks). In each, the plan asked for next is made, and nobody restarted anything.

**Exercise 2: Gemini answers 429.** Expected: the roadmap asks whether it backs off or fails the trip. Found: neither, by design — one request, then the other provider; the plan in its usual time. With both providers refusing: Groq's client waits the 2 seconds its `Retry-After` says and tries once more; intent parsing is logged `failed`; the plan is made from the trip's own fields; a refinement nobody could classify is run as a full re-plan. Nothing was changed.

## Dev B — the builder's model, and Redis

**Exercise 3: the model that writes the itinerary never answers.** Expected by the roadmap: `failed` within 60 seconds. Found: after 15 minutes, when the exercise stopped waiting, the trip was still `planning` and the client was still waiting on its first request. The builder made its client with no timeout; the wrapper hands that to the SDK as `None`; `None` is no timeout.

Changed (#265, #266): 20 seconds a read, one more try, 30 seconds for the whole reply; a build that timed out is not built again; the traveller is told the model did not answer in time, on the stream and from `GET /status`; and a planning run as a whole has a deadline of five minutes.

**Exercise 4: Redis killed for 30 seconds under twenty open streams.** Expected by the roadmap: clients reconnect; no orphaned subscriptions. Found: both, as hoped — every stream ended within seconds, every page was connected again within ten seconds of Redis returning by the page's own backoff, `CLIENT LIST` showed one subscribed connection per open stream and none after the pages had gone. Also found: the plan in flight took as long as the outage; `POST /plan` meanwhile was a 500.

Changed (#267–#269): a run does not wait on a Redis that is away; Redis away is a 503 in words; a trip marked `planning` always gets its run; and — the consequence of the first of these — a planned trip whose saved state is gone is changed by a full re-plan instead of being refused.

## Found and fixed along the way

**Putting a dead tool server away cancelled somebody else.** The first version of the fix closed the dead session from the call that found it dead. Run against the real process with three calls in flight: `CancelledError: Cancelled via cancel scope … by <Task call_tool()>`. The library's scopes cancel the task that entered them when another leaves them. One task now owns a server (#262).

**A call sent just after the pipe closed never came back.** With the server killed again while it was being started for three calls: two were told, the third hung in `send_request`. The pipe is watched (#262).

**The owner task was cancelled by the library when a write to a dead server failed** — and left the library's generator half-closed, to be finalised by the event loop at exit from yet another task. The scopes are now entered with `async with`, in order, by the owner.

**A lock that has been waited on belongs to its event loop.** The client's lock was made once per process; calls now wait on it, and the third integration test in a row failed with "bound to a different event loop". One per loop.

**The second kill did not test what it was meant to.** Searches warmed when a trip is made finish within milliseconds of each other; "after the flights, before the hotels" had to be arranged — a trip whose flights are cached and whose hotels are not — and the server kept dead.

**`GET /status` said one thing and the stream another.** The page was told "the model that writes it did not answer in time"; a page loaded afterwards was told "The model did not answer within 30 s." — the builder's note to itself. The run now writes down what it told the traveller, and `GET /status` reads that.

**The fix for a slow run made a refinement fail.** Once a run no longer waited for Redis to return, it ended during the outage and could not save the state a refinement needs: 409, "Run POST /plan first", for a trip with a plan. Predicted in the hypotheses, not seen before the fix, seen at once after it (#269).

**The script left its own streams open.** Three pages from the first three exercises were still connected when the Redis exercise counted subscriptions: 24 for 21 streams. Not the app's.

**The first run of the fixed app was made by a sleeping laptop.** Lid closed, on battery, from 08:04: every duration in those records was meaningless. The script now records whether the machine slept (#271); those records are not kept.

**A by-hand command written before it was run.** `HOW_TO_RUN.md` first said `docker exec … pkill -9 -f tool_server`. The image has no `pkill`.

## Live results

Every exercise on the tree as committed, with the machine awake (20:37–20:44 IST, 13 October): **7 of 7 as expected** — the table is the report's section 4, the records `docs/chaos/after/`. In short: the tool server killed once → the plan whole, on a new server; kept dead → `CONNECTION_REFUSED` rows, the right reason, a partial plan that says what it lacks; the builder's model silent → the trip `failed` after 36.4 s (the client stayed 20 s, then 9.5 s); Redis killed for 30 s → the build took 7 s where it had taken 32, a plan asked for meanwhile was a 503 in words, all 20 streams were back within 10.4 s of Redis, and `CLIENT LIST` showed 21 subscriptions for 21 streams and none once the pages had closed.

## Tests

**1,253 unit + contract and 74 integration pass on the tree as committed.** New in this phase: 53 unit, 9 integration.

- **`tests/unit/test_phase31_recovery.py` (34)** — the fixes: a call that finds the tool server gone starts another and asks again, for each way a dead pipe shows; a server that dies again is not asked a third time; one that will not start is the same thing to its caller; an error that is not the server dying is not taken for it; only the call that holds the dead session lets go of it; a call is not waited for once the pipe has closed; every start counted; the traveller's words; searches that could not run are not a destination with nothing in it; the label and who may write it; the builder's deadline, its client's timeout, what is and is not built again; the publisher that does not wait on a dead Redis; a run started whatever Redis does; Redis away as a 503.
- **`tests/unit/test_phase31_harness.py` (19)** — the failing server asked directly, and **by the providers' own clients**: Google's told 429 asks once and the other model answers; Groq's given no answer is stopped by the builder's deadline. Which model a fault applies to; that a fault never points a client anywhere but this machine; that nothing in `src/` knows about faults; the script's page held to `sse.ts`, its numbers to the roadmap's, its integrity questions, its refusal of any stack but the stand-ins'.
- **`tests/integration/test_phase31_chaos_integration.py` (9)** — the real tool server sent `SIGKILL` between calls, under calls in flight (and nobody cancelled), and repeatedly; through the HTTP API on a real database: a plan saved saying what it lacks, a trip failed because nothing could be searched, a builder that never answers, a run stopped at its deadline, a planned trip whose state Redis has lost still changed.
- Changed: `test_phase29_wiring.py` (the fake session can be closed: the task that opens it closes it), `test_phase15_refinement.py` (no state *and* no itinerary is the 409).

**What was still owed when this was first committed** — the laptop's battery was at 3 %:

- **The browser suite: run next, 58 of 60 passed, 1 failed, 1 did not run.** The failure was a test that held the sentence #263 replaced: with every provider down, `e2e/polish.spec.ts` expected "No flights, places to stay or things to do were found for Shimla" and the page now says "None of the searches could be run just now…". The test was changed to the new sentence. **Its re-run did not get as far as the assertion** — sign-in against the stub stack did not complete on a machine at 2 % battery — so that one test, and the one after it in the file that did not run, are not yet seen passing on this tree.
- **The break-on-purpose checks: not run.** The list is written (97 breakages).
- **Phase 27's and Phase 29's checks on real providers: not run on this tree.** In particular the real Groq model has not yet been seen writing an itinerary under the new 20-second / 30-second limits; the measured builds took 5 to 9 seconds.

### Break it on purpose

Not run yet: see above. The list is written (97 breakages of the fixes, the faults, the failing server and the script).

## Done criterion checklist

| | Criterion | |
|---|---|---|
| ✅ | Chaos exercise 1: kill the MCP server process mid-planning | three variants; `docs/chaos/after/mcp-kill-*.json` |
| ✅ | `ToolError(code="CONNECTION_REFUSED")` propagates; the orchestrator marks the affected agent `failed` | when the server stays dead; when it does not, the search is asked again of a new one and nothing fails |
| ✅ | …continues with partial results, clearly labelled in itinerary | `structured_data.missing` — in what is saved and in `GET /itinerary`; the page and the PDF do not show it yet (#270) |
| ✅ | `agent_runs` shows `status = "failed"` with error reason | `{"code": "CONNECTION_REFUSED", "error": "The tool server could not be reached."}` |
| ✅ | Chaos exercise 2: Gemini 429 — backs off, or fails the trip? | neither: the other provider is asked at once; the run completes |
| ✅ | Both documented: hypothesis before running, actual behaviour, fixes | the report, sections 1 to 3 |
| ✅ | Chaos exercise 3: builder times out — trip `failed` within 60 s, not hung | see "Live results" |
| ✅ | Chaos exercise 4: Redis killed 30 s under active streams — clients reconnect | by the page's own backoff; the script's pages reconnect as `sse.ts` does, and a test holds the two to each other |
| ✅ | No orphaned Redis pub/sub subscriptions after restart (`CLIENT LIST`) | one per open stream after the restart, none once the pages have closed |
| ✅ | No data corruption in any case | seven counts of rows that must not exist, after every exercise: all zero |
| ◻ | …in a real browser | the reconnect was exercised by a client that imitates the page's hook, not by a browser (see below) |

✅ done and seen on the stack · ◻ not done

## Known limits

- **One laptop, stand-ins, no deployment.** What is broken is real — a process, a container, a provider's own client — but the providers are not, and nothing here says what a hosted Redis or a platform's restart looks like from inside.
- **The reconnect was not watched in a browser.** The script's pages reconnect with the hook's own delays (1, 2, 4, 8, 16, 30 s), and a test holds those to `src/frontend/src/lib/sse.ts`; `EventSource` itself, and the page falling back on `GET /status`, were not exercised under a killed Redis.
- **`missing` is not shown on the page or in the PDF** (#270). The page shows the failed search and its Retry, as it did.
- **One subscription per page still** (#270): Phase 30's limit of connections is where it was.
- **Redis *frozen* rather than killed was not tried.** Nothing gives a Redis command a timeout of its own except publishing in a run (2 s) — a Redis that accepts connections and never answers would hold a request until TCP keep-alive gives up, about a minute.
- **A tool server that is alive and stuck is only bounded by the run's five minutes.** A tool call has no timeout of its own: a search may legitimately wait a minute for a rate limit.
- **The exercises' Redis keeps nothing on disk**, unlike the hosted one it stands in for: "comes back empty" is harsher than what a deployment would see, and is what found #269.
- **Only the builder and the extractor were given timeouts here.** The other model calls had them already (15 s for a short prompt, 8 s for local tips).
