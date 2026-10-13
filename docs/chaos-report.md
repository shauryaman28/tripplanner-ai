# Chaos report — Phase 31

**Status: done, on one machine.** Four things the app depends on are broken on purpose while it plans a
trip, and what it then does is written down beside what was expected: three of the four found
something a traveller would have met, each was fixed, and all four now go as expected (section 4).

> Like Phase 30's load test, all of it is done on **one laptop**, on the production-like stack of
> Phase 27 — the image that is deployed — with stand-ins for the models and the providers
> (`loadtest/standins/`). What is broken is real: a process is killed, a container is stopped, a
> provider's own client is pointed at a server that says 429 or never answers. There is no
> deployment, and none of this may be done to one.

## 1. Written before anything was broken

Written 2026-10-13 07:39 IST, from reading the code, before any exercise — or the harness that runs
them — had been run once. It is kept as it was written; what happened is in the sections after it.

### Exercise 1 — the tool server is killed in the middle of a plan

*The roadmap expects:* `ToolError(code="CONNECTION_REFUSED")` propagates, the orchestrator marks the
affected agent failed and continues with partial results, clearly labelled in the itinerary;
`agent_runs` shows `status = "failed"` with the reason; no data corruption.

The backend has one tool server: a subprocess it talks to over pipes, with all three searches in
it. It will be killed (`kill -9`) at two moments — before any search has answered, and just after
the flight search has.

What I expect, and why:

1. **The searches under way fail at once; nothing hangs.** The MCP library answers every pending
   request with "Connection closed" when the pipe closes, and `call_tool` turns any exception into a
   `ToolError`. Its code will be `MCP_CLIENT_ERROR`, not `CONNECTION_REFUSED`: the app has no such code.
2. **Each search that failed is a `failed` row in `agent_runs` with the error**, and a `failed` event
   on the stream — but worded for nobody: "MCP client exception: Connection closed". The traveller's
   words (`src/ai/utils/failures.py`) have no entry for this code. It will be offered for Retry.
3. **Killed before any search answered:** all three fail, so the run ends in `nothing_found` and the
   trip is `failed` — with the wrong reason: "No flights, places to stay or things to do were found
   for …", as if the destination had nothing.
4. **Killed after the flights were found:** the hotel and attraction searches fail, the plan is built
   from the flights alone and the trip is `completed`. I have found nothing that labels the saved
   itinerary itself as partial; I expect the label to be only on the page (`GET /status`: one search
   of three done, two failed with a Retry) and not in the itinerary or its PDF.
5. **It does not recover — the main thing I expect to have to fix.** The client keeps its session in
   a module global that is set once and never cleared (`src/ai/mcp_client/client.py`). After the
   kill every later tool call fails the same way: the next plan finds nothing, cache warming fails
   quietly, and a Retry fails — until the backend itself is restarted. Nothing starts a new tool
   server.
6. **No corruption.** Every step commits its own row; I expect no trip left in `planning`, no
   itinerary without a completed trip, no half-written rows.

*What would show me wrong:* a plan that never ends; a 500; a trip stuck in `planning`; or a later
plan that works without a restart.

### Exercise 2 — Gemini answers 429

*The roadmap asks:* does the orchestrator back off correctly, or fail the whole trip? *Acceptance:*
backoff fires, the run completes or escalates gracefully; no corruption.

Gemini's own client, made as the app makes it (`max_retries=1`, a 15-second timeout), will be
pointed at a server that answers every request 429 in Gemini's words, with the "retry in 36 s" its
free tier sends. Then the same with Groq's small model — the one asked instead — answering 429 too.

What I expect, and why:

1. **No backoff, by design — a second provider instead.** `llm.ask` makes one attempt at Gemini and,
   whatever goes wrong, asks Groq's small model. I expect the client to send one request, or two
   (I do not know whether `max_retries=1` means one try or one more), and **not** to wait the 36
   seconds the error asks for: the fallback within a second or two, the `intent_parsing` row
   `completed`, the plan done in its usual 10 to 16 seconds.
2. **Both answering 429:** Groq's client (`max_retries=1`) asks once more after the 2 seconds its
   `Retry-After` says, then gives up — some 2 to 4 seconds. `intent_parsing` is logged `failed`, the
   run "carries on with the fields it already has", and the plan is made: a trip's own fields are
   enough. A refinement asked for in that state is not refused: the classifier defaults to a full
   re-plan.
3. **No corruption**, and nothing on the request path waits long enough to matter.

*What would show me wrong:* the client sleeping the 36 seconds; more than two requests; a trip that
fails because a model for a short prompt was unavailable.

### Exercise 3 — the model that writes the itinerary never answers

*The roadmap expects* (it says Claude Haiku; here the builder's model is Groq's large one): the trip
times out and is marked `failed` within 60 seconds — not hung indefinitely.

Groq's own client, made as the builder makes it, will be pointed at a server that reads the request
and never sends anything back.

What I expect, and why:

1. **It fails the 60 seconds by a wide margin: about twelve minutes.** The builder makes its client
   with no timeout (`ChatGroq(model=…, temperature=0, max_tokens=8192)`), so the SDK's defaults
   apply: 60 seconds for a read, and two more tries — about 180 seconds before one build gives up
   with `LLM_ERROR`. The orchestrator then builds again, up to three more times
   (`MAX_EVALUATOR_RETRIES`): 4 × 180 s ≈ 12 minutes until `builder_failed` marks the trip.
2. **Nothing bounds a planning run as a whole.** For those twelve minutes the trip says `planning`,
   the page shows the itinerary being written, and another `POST /plan` is refused (409).
3. **It does end**, `failed`, with a reason like "LLM call failed: Request timed out." — and a plan
   asked for afterwards, with the model answering again, works.
4. **No corruption:** no itinerary is saved, the searches' rows stand.

*What would show me wrong:* a failure inside 60 seconds (a deadline I have not seen), or a run that
never ends.

### Exercise 4 — Redis is killed for 30 seconds under open event streams

*The roadmap asks:* do clients reconnect? Do orphaned pub/sub subscriptions remain? *Acceptance:*
clients reconnect via `EventSource` backoff; no orphaned subscriptions after the restart
(`CLIENT LIST`).

Twenty streams will be held open by a client that reconnects the way the page's hook does (1, 2, 4,
8, 16, 30 seconds), and one by the page itself in a real browser; a plan will be started a few
seconds before the Redis container is stopped, and the container started again 30 seconds later.
This stack's Redis keeps nothing on disk — unlike the hosted one it stands in for — so it comes
back empty.

What I expect, and why:

1. **Every stream ends within a few seconds of the kill** — the subscription tries to reconnect
   three times, then the server closes the stream: an HTTP response that ends, not an error.
   **The page's own backoff reconnects it**: attempts fall about 1, 3, 7, 15 and 31 seconds after
   the stream ended, each getting an empty stream back while Redis is away, so every stream is
   connected again within a few seconds of Redis returning — at the attempt near the 31st second —
   or, if that one is just too early, 30 seconds later.
2. **No orphaned subscriptions.** The old ones die with the old Redis; the server's generators have
   ended. After the restart I expect exactly one subscribed connection per open stream, and none
   once the clients have gone.
3. **The plan in flight is still made.** Publishing an event "must never fail a planning run" and
   the tool server's cache treats a Redis error as nothing found, so the itinerary is saved to
   Postgres. But what was published while Redis was away is gone — among it, possibly,
   `planning_complete` — and the page learns of the plan from polling `GET /status`.
4. **What the wipe takes with it:** the state a refinement needs is saved to Redis at the end of a
   run. If that save falls into the outage, or the restart empties Redis, the trip has an itinerary
   and `POST /refine` answers 409 "nothing to refine" until it is planned again.
5. **`POST /plan` during the outage is a 500**, not a refusal in words — the first thing it does is
   write the conversation to Redis — and the trip is left as it was, not stuck in `planning`.
6. **No corruption in Postgres.**

*What would show me wrong:* a stream that stays open and silent after Redis is back; more
subscribed connections than open streams; a plan that fails because Redis was away; a trip left in
`planning`.

## 2. What happened — the app as Phase 30 left it

Run 2026-10-13 between 07:44 and 08:04 IST, on commit `485d1e4` with nothing of the app
changed — the app in the stack was the image built from Phase 30's tree (it dates from 02:42 that
night; the build at 07:41, before the first line of `src/` was touched, found nothing in it to
rebuild and only added the stand-ins' fault server beside it). Written down at 08:04 IST, as the last of them ended; the fixes of section 3 were being written while the
later ones ran, and none was in what ran. Every record is in [chaos/before/](chaos/before/).

| Exercise | Expected (section 1) | What happened |
|---|---|---|
| 1 — tool server killed before any search answered | fails at once, wrong reason, never recovers | **as expected, and blunter**: the three searches failed in 2 to 5 ms with the reason `"MCP client exception: "` — nothing after the colon; the trip `failed` 6.7 s after the kill with "No flights, places to stay or things to do were found for Udaipur"; **the next plan failed the same way, and so would every one after it** |
| 1 — killed after the flights were found | a plan from the flights alone | **not what was set up**: the three searches had been warmed and finished within 30 ms of each other, so the kill came after all of them — the plan was whole. The plan after it found nothing: the tool server was never started again |
| 2 — Gemini answers 429 | one or two requests, no 36-second wait, the plan made | **as expected**: 1 request to Gemini, the other model asked at once, the plan in 14.9 s |
| 2 — the model asked instead answers 429 too | intent parsing `failed`, the plan made, a refinement not refused | **as expected**: 1 request to Gemini, 2 to Groq 2.0 s apart (its `Retry-After`), the plan in 13.8 s, the refinement run as a full re-plan |
| 3 — the builder's model never answers | `failed` after about twelve minutes | **wrong, and worse**: after 900 s — when the exercise stopped waiting — the trip was still `planning`. The client had sent **one** request and was still waiting for its answer: no timeout at all. It would have waited for as long as the process lived |
| 4 — Redis killed for 30 s | streams end and reconnect; no orphans; the plan made; `POST /plan` a 500 | **as expected, with one thing I had not thought of**: all 20 streams ended 5.8 s after the kill and were connected again 8.3 to 10.3 s after Redis was back; `CLIENT LIST` showed 21 subscribed connections for 21 streams, and none once the pages had closed; `POST /plan` during the outage was a bare 500. **But the plan in flight took as long as the outage did** |

### Where section 1 was wrong

- **Exercise 3: "about twelve minutes" should have been "never".** I reasoned from the Groq SDK's own
  default — 60 seconds for a read, two more tries. That default is not what the builder gets: with no
  timeout given, the LangChain wrapper hands the SDK *none*, and a request that is never answered is
  waited for without end. One request in 15 minutes; `agent_runs` has no row for the build, the trip
  says `planning`, a second `POST /plan` is refused with 409, and the only way out is to restart the
  backend — whose start-up recovery then marks the trip. The roadmap's "not hung indefinitely" was
  exactly the case.
- **Exercise 4: the run in flight was not untouched — it was slowed to the length of the outage.**
  The itinerary's build took 32 s
  where it takes 7: each piece of it is published as it is written, and every publish to a Redis that
  is not there spent a second or two reconnecting before it gave up. The run ended just after Redis
  came back. So the state a refinement needs *was* saved, and `POST /refine` answered 200 — my
  expected 409 did not happen, for a reason I had not seen. A longer outage would simply have meant a
  longer plan.
- **Exercise 1, the second kill, did not test what it was meant to.** Searches that are warmed when a
  trip is made finish together; "after the flights, before the hotels" has to be arranged — a trip
  whose flights are already cached and whose hotels are not — and the tool server has to *stay*
  dead for the plan to be partial. The exercise was rebuilt that way (three variants: killed once;
  kept dead from the start; kept dead from the moment the flights are in).
- **Exercise 1: the reason was not "Connection closed" but nothing.** `ClosedResourceError` has no
  message; the traveller's page said `MCP client exception: `.

What section 1 got right: nothing hung in exercises 1, 2 and 4; no row in Postgres was left half-done
in any of them; Gemini's 429 costs nothing; clients reconnect by themselves and no subscription is
orphaned. In exercise 3 one trip was left `planning` with nothing planning it.

## 3. What was changed

Each change answers one thing section 2 found; each is held by a test, named beside it.

### The tool server (exercise 1)

| Found | Changed | Held by |
|---|---|---|
| A killed tool server was never started again: the client kept its dead session for the life of the process | A call that finds the connection gone lets go of that session — the next call starts a new server — and **asks once more itself**, of the new one. A search changes nothing, so it can be asked twice; a server that dies again under the second asking is not asked a third time | `test_a_killed_tool_server_is_replaced_and_the_search_that_found_it_dead_is_answered` (the real process, `SIGKILL`) |
| The reason was `"MCP client exception: "` | What the caller is told has a name, `CONNECTION_REFUSED` — the roadmap's — and words for the traveller: "The search service stopped answering. Try again in a moment." | `test_the_traveller_is_told_the_search_service_stopped…` |
| "No flights, places to stay or things to do were found for Udaipur" | When none of the three searches could be *run*, the trip fails saying that — "nothing is wrong with your trip" — and not that its destination is empty | `test_with_no_search_able_to_run_the_trip_fails_for_that_reason…` |
| A plan made without a search was labelled only on the page that happened to be open | The saved itinerary says what it lacks and why: `missing: [{search, what, why, retryable}]` — written by the planner, never taken from the model's reply | `test_a_plan_made_without_a_search_is_saved_saying_so` |
| Nothing counted a tool server dying | `mcp_tool_server_starts_total`: one is the process starting, any more is a death | `test_every_start_of_the_tool_server_is_counted` |

Two things were found while making that change, by killing the real process under the new code
rather than a mock of it:

- **Putting a dead server away cancelled somebody else.** The library's pipes and session are scopes
  that must be left by the task that entered them; left by another, they cancel the one that
  entered — which was whichever request had needed a search first. Under load that is a planning
  run. One task now owns a tool server from start to end and does nothing else.
- **A request sent a moment after the pipe closed waited for ever.** The library fails the requests
  that are waiting when the pipe closes; one written just afterwards is neither answered nor
  failed. Seen with a server killed again while it was being started for three calls at once: two
  were told, the third hung. The pipe is now watched by something a call waits on beside its
  answer.

### The model that writes the itinerary (exercise 3)

| Found | Changed | Held by |
|---|---|---|
| The builder's client had no timeout at all: one unanswered request held a trip `planning` for as long as the process lived | 20 s for a read, one more try, and **30 s for the whole reply** whatever the client is in the middle of. A plan is written in 5 to 9 s | `test_groqs_own_client_given_no_answer_is_stopped_by_the_builders_deadline` (Groq's own client, a server that never answers) |
| A failed build is built again up to three times | A build that *timed out* is not: the next would wait as long, and the traveller with it | `test_a_build_that_timed_out_is_not_built_again…` |
| — | The traveller is told "the model that writes it did not answer in time", on the stream and from `GET /status`; the row keeps what the client raised | `test_a_builder_whose_model_never_answers_fails_the_trip_at_the_deadline_and_says_why` |
| The preference extractor's client had none either (it runs after the plan is the traveller's) | 10 s, no second try | `test_the_builders_client_is_made_with_a_timeout_and_one_more_try` |
| Nothing bounded a planning run as a whole | 300 s, then the trip is marked and the page told: a backstop for whatever hangs that nobody has thought of | `test_a_run_that_never_comes_back_is_stopped…` |

### Redis (exercise 4)

| Found | Changed | Held by |
|---|---|---|
| A run in flight took as long as the outage: every piece of the itinerary was published, and each publish to a dead Redis spent a second or two reconnecting | After a publish fails none is tried for 5 s, and one is given 2 s at most; the events that end a run are always tried. What is not published was lost either way — the page reads `GET /status` | `test_a_run_does_not_wait_on_a_redis_that_is_not_there` |
| `POST /plan` during the outage: 500, "something went wrong on our side" | 503 with `Retry-After`, "The planner cannot be reached for a moment. Nothing is lost — please try again shortly." | `test_redis_being_away_is_answered_as_something_that_passes` |
| Between marking a trip `planning` and starting its run, two writes to Redis: Redis going in between would leave a trip planned by nobody | Once the trip says `planning` the run is started whatever Redis does | `test_a_redis_that_goes_away_as_a_run_begins…` |
| With the first fix in, the plan finishes *during* the outage — and cannot save the state a refinement needs. `POST /refine` then answered 409 "Run POST /plan first" for a trip that had a plan | A planned trip whose saved state is gone is changed as a **full re-plan from the trip as Postgres has it**. (The same state expires after a day: that was a 409 too.) | `test_a_planned_trip_whose_state_redis_has_lost_can_still_be_changed` |

That last one is the fix for the first one's consequence: section 1 predicted the 409, section 2
did not see it — because the slow run happened to outlast the outage — and it appeared as soon as
the run stopped being slow.

### Gemini's 429 (exercise 2)

Nothing. The app's answer to a 429 on the request path is another provider, not a wait, and it
does what it was built to: one request, no 36-second sleep, the plan in its usual time; with both
providers refusing, intent parsing is logged `failed`, the plan is made from the trip's own fields,
and a refinement nobody could classify is run as a full re-plan. The roadmap's "backoff fires" is
Groq's client waiting the two seconds its `Retry-After` says before its one further try.

## 4. After

Every exercise again, on the tree as committed, 2026-10-13 20:37–20:44 IST, with the machine awake
(each record says so: `asleep_s: 0`). Written at 21:05 IST. Records: [chaos/after/](chaos/after/).

| Exercise | Before | After |
|---|---|---|
| 1 — the tool server killed once, a second into a plan | every search failed; the trip failed for its destination; nothing worked until a restart | the plan is whole: 3 of 3 searches — asked of a new tool server (pid [25] → [48]); the next plan too |
| 1 — kept dead from before any search answered | — | 3 `failed` rows, each `CONNECTION_REFUSED`; the trip `failed`: "None of the searches could be run just now, so there is no…"; the next plan works |
| 1 — kept dead from the moment the flights are in | — | the plan made from 2 of 3 searches; `activities_agent` failed with `CONNECTION_REFUSED`; the itinerary says `missing: things to do` and why; the next plan works |
| 2 — Gemini answers 429 | 1 request, the plan in 14.9 s | 1 request, the plan in 14.9 s |
| 2 — and the model asked instead | the plan in 13.8 s, the refinement a full re-plan | the plan in 12.9 s, the refinement a full re-plan |
| 3 — the builder's model never answers | still `planning` after 900 s, one request, no end | **`failed` after 36.4 s**: the client asked 2 times and stayed 20 s and 9.5 s — its own timeout, then the deadline; the same trip planned again once the model answered |
| 4 — Redis killed for 30 s | streams back 8.3–10.3 s after Redis; the build took 32 s; `POST /plan` 500 | streams back 3.5–10.4 s after Redis; **the build took 7 s** — the plan was finished while Redis was still away (`GET /status`: completed, planning); `POST /plan` **503**, "The planner cannot be reached for a moment…"; the trip changed afterwards by a full re-plan |

In every exercise, before and after: `CLIENT LIST` showed 21 subscribed connections for 21 open streams after the restart and
0 once the pages had closed; and the seven counts of rows that must not exist were all zero —
except, before, the one trip the silent model left `planning`.

### Against the roadmap's acceptance

| | |
|---|---|
| MCP server kill → graceful degradation, `agent_runs` shows `status = "failed"` with error reason | yes, when the server stays dead: `{"code": "CONNECTION_REFUSED", "error": "The tool server could not be reached."}`. When it does not, nothing fails at all |
| Gemini 429 → backoff fires, run completes or escalates gracefully | the run completes; what "fires" is a second provider, and Groq's client waiting its `Retry-After` |
| No data corruption in either case | none: seven counts, all zero |
| Builder timeout → trip marked `"failed"` within 60 s | 36.4 s from `POST /plan` |
| Redis kill → clients reconnect via `EventSource` backoff | all 20, 3.5 to 10.4 s after Redis returned — by a client that waits as the page's hook does; not by a browser |
| No orphaned Redis pub/sub subscriptions after restart (`CLIENT LIST`) | 21 for 21 streams; 0 with every page closed |

## 5. What this does not show

- **A deployment.** One laptop; stand-ins for the models and the providers; a Redis that keeps nothing
  on disk, where the hosted one does.
- **A browser.** The reconnect is the script's imitation of `src/frontend/src/lib/sse.ts` — held to
  it by a test — not `EventSource` in a page.
- **A Redis that is frozen rather than dead**, a tool server that is alive and stuck, a provider that
  answers slowly rather than never. The run's five-minute deadline is all that bounds the second.
- **The label on the page or in the PDF**: `missing` is in the saved itinerary and in
  `GET /itinerary`; neither shows it yet.
- A first run of the fixed app was made by a laptop asleep in cycles (lid closed, on battery, from
  08:04). What happened in it matched what is above; its times meant nothing, and it is not kept.
