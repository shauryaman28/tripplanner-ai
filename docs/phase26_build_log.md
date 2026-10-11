# Phase 26 — LangSmith Tracing & Token Costs

**Status: ✅ Complete — on everything that can be checked without a LangSmith account. One step needs a key, and is yours: [it is at the end](#the-one-step-that-needs-a-key).**
**What the phase delivers (roadmap):** Every agent run produces a complete trace. Re-planning and evaluation branches visible. Filterable by `trip_id`.

Decisions: `DECISIONS.md` #173–#188. Three things are worth knowing before reading on.

- **There is no LangSmith key in this build's `.env`.** The roadmap's acceptance is four sentences that end "in LangSmith". They are verified here on what a trace *is* — the records the tracing library uploads — read by the tests, and by a local receiver on a real run of the real backend. Nothing here was seen in LangSmith's own UI (#179).
- **The dollar figures are list prices from price trackers, not from the providers' own pricing pages.** One table, three models, correctable in `.env` (#181).
- **At those prices the roadmap's alert — $0.10 for one trip — is about thirty-four plans, not ten evaluator retries.** The roadmap's test passes at a frontier model's price; a second test says what it means at this app's (#184).

## What was built

```
src/ai/tracing.py                      ← NEW: on/off from the settings; what a trace is tagged with; root / span / step;
                                         what is taken out of an upload (scrub); a shutdown that waits five seconds, not for ever
src/ai/usage.py                        ← NEW: the meter (metered), the one recorder that reads every model reply's tokens,
                                         the price table, Usage.token_count / cost_usd
src/ai/trace_tree.py                   ← NEW: the records LangSmith is sent, put back together as trees; render() as text
scripts/trace_receiver.py              ← NEW: a local stand-in for LangSmith — prints each trace, /traces?trip_id=, /records
src/ai/utils/run_logger.py             ← timed_run() also meters; a row carries its tokens and cost; the over-cost alert
src/ai/orchestrator/orchestrator.py    ← a run is tagged with its trip and user; "re-plan N: cheaper flights";
                                         "evaluator retry N"; a change to a plan is a trace with the graph's steps
src/ai/mcp_client/client.py            ← every tool call is a span ("mcp:search_flights"); a tool that failed is a failed span
src/ai/agents/flight_agent.py, hotel_agent.py, activities_agent.py ← each agent's graph is named for it; usage on its row
src/ai/agents/destination_intelligence.py, preference_extractor.py, src/ai/builder/builder.py ← usage on their rows; the
                                         tips agent is a span of its own
migrations/versions/006_agent_run_tokens.py ← NEW: agent_runs.token_count, agent_runs.cost_usd
src/backend/app/models/agent_run.py, schemas/agent_run.py ← the two columns, and in GET /trips/{id}/runs
src/backend/app/schemas/cost.py        ← NEW: the shape of the cost summary
src/backend/app/api/routes/admin.py    ← GET /admin/cost-summary?since=
src/backend/app/api/routes/trips.py    ← the refinement classifier is timed, traced and logged; /replan says what it is;
                                         the timeline carries tokens and cost
src/backend/app/core/config.py         ← LANGCHAIN_TRACING_V2, _API_KEY, _PROJECT, _ENDPOINT; LLM_COST_ALERT_USD; LLM_PRICES
src/backend/app/main.py                ← tracing configured at startup, flushed at shutdown; version 0.26.0
.env.example                           ← the Observability section, with what a trace uploads said plainly
src/frontend/src/lib/types.ts          ← AgentRun.token_count / cost_usd (optional; no page shows them yet)

requirements.txt                       ← langsmith, which the app now imports itself (it had only come along with LangChain)

tests/fakes.py                         ← a chat model that answers through LangChain with token counts (also streamed);
                                         a LangSmith that keeps what it is sent — as JSON batches, as multipart, or
                                         compressed, the way a current LangSmith takes it — and sends nothing anywhere
tests/conftest.py                      ← tracing is off before and after every test; the cost limit and the prices
                                         are the code's own, whatever this machine's .env says
tests/unit/test_version.py             ← NEW: the app's version is the phase the README says
```

## The path of a trace

```
POST /trips/{id}/plan
  └─ OrchestratorAgent.run(kind="plan")
       ├─ tracing.run_config(...)  → {"run_name": "plan trip", "metadata": {trip_id, user_id, turn, kind}, "tags": […]}
       └─ graph.ainvoke(state, config=that)          LangGraph makes the root and a span per node;
            ├─ intent_parsing                         LangChain makes a span per model call (prompt, reply, tokens);
            │    └─ ChatGoogleGenerativeAI  [llm]     the root's metadata is handed down to every one of them
            ├─ run_flight
            │    └─ flight_agent  → search_flights → mcp:search_flights  [tool]      ← tracing.span in the MCP client
            ├─ budget_decision
            ├─ run_flight
            │    └─ re-plan 1: cheaper flights  → flight_agent → …                   ← tracing.span in _search_flights
            ├─ …
            ├─ evaluate
            ├─ retry_dispatch
            │    └─ evaluator retry 1  → activities_agent → …                        ← tracing.span in retry_dispatch_node
            └─ build_itinerary → ChatGroq  [llm]

langsmith's background thread ── POST /runs/multipart ──▶ LangSmith   (POST /runs/batch to scripts/trace_receiver.py,
                                  each run without db /               which asks for plain JSON; nowhere in a test)
                                  publish_fn / on_complete (scrub)

Meanwhile, for the database:
  timed_run()  opens a meter ─┐
  a model replies ────────────┤ the one recorder credits the reply's tokens to the innermost open meter
  log_agent_run(usage=…)  ────┘ → agent_runs.token_count, cost_usd → is the trip past LLM_COST_ALERT_USD? → log once
```

A refinement or a retried search does not invoke the graph — it calls nodes one at a time — so `tracing.root`
opens its trace and `tracing.step` makes each node call the span the graph would have made of it.

## Dev A — the traces

**Switching it on.** Two lines in `.env`: `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY=…`. The app reads them
itself and tells the tracing library (`tracing.configure`), because this app never exports `.env` into the process
environment, which is where LangChain would have looked: started with `uvicorn`, the roadmap's two lines alone would
have traced nothing (#173). The log says which it is:

```
[TRACING] LangSmith tracing is on — project 'tripplanner-ai', sent to LangSmith
[TRACING] LangSmith tracing is off (LANGCHAIN_TRACING_V2 is not true)
[TRACING] off: LANGCHAIN_TRACING_V2 is true but LANGCHAIN_API_KEY is empty. Set a key in .env, or switch tracing off.
```

**The acceptance, point by point** — each on the records the library uploads:

| Roadmap | Where it is checked |
|---|---|
| One planning run produces a complete trace | `test_a_planning_run_is_one_trace_with_a_span_for_every_step`: one root, "plan trip", and beneath it the graph's ten nodes in the order the run visited them; `test_a_model_call_is_a_span_with_its_tokens_…`: each model call a span with its tokens, and every step above it showing their sum |
| Every node execution is a span: input, output, duration, token count | the same tests, plus the live trace below — every line has a duration, and tokens wherever a model was asked |
| Tagged with `trip_id` and `user_id` | `test_every_span_is_tagged_with_its_trip_and_its_user`: all of a trace's spans, not just the root |
| Filtering by `trip_id` returns only that trip's traces | `test_filtering_by_trip_returns_only_that_trips_traces`: two trips planned, each filter returns its own; over HTTP against the receiver in the integration tests |
| Re-planning branch appears as a distinct span | `test_a_replanning_branch_is_a_distinct_span`: flights too dear for the budget → `run_flight` twice, the second holding "re-plan 1: cheaper flights" with the cap and stops it searched with |
| Evaluator retries appear as numbered spans | `test_evaluator_retries_are_numbered_spans`: a plan sent back twice → "evaluator retry 1", "evaluator retry 2", each saying which checks failed |

**Three roads, one trace.** The tracing library asks a server how it wants its runs, and has a road of its own
for each answer. The local stand-in says "JSON batches" (`POST /runs/batch`), because those can be read with
nothing but `json`. Told nothing, the library sends multipart (`POST /runs/multipart` — a part for each run, and
one for each of its big fields); that is what it did on the live run against an address that never answered. And
to a server that says it takes compressed uploads it sends that multipart zstd-compressed, built by different
code again — the road a current LangSmith gets, going by the library's own log line when the server does *not*
say so ("Run compression is not enabled. Please update to the latest version of LangSmith"). So the same plan is
run down all three: `test_sent_the_way_langsmith_itself_is_sent_it_…` reads the multipart upload back, plain and
compressed, and finds the same trace — the same ten steps, the tokens, the trip's id on every span — and none of
the live objects. The encoding is the library's work, not this project's; what the tests hold is that nothing
here, and above all not the taking-out of the live objects, depends on which road is used.

### The live run (the real backend, real models, the local receiver)

`scripts/trace_receiver.py` on port 1984; the backend started with `LANGCHAIN_TRACING_V2=true`,
`LANGCHAIN_API_KEY=local-stand-in`, `LANGCHAIN_ENDPOINT=http://localhost:1984`; the demo account; 2026-10-11.

**A plan** — Jaipur, 2 travellers, 4 days, ₹70,000 — completed in 8.3 s. What the receiver printed:

```
plan trip   trip 45a86c91  user 8ce59c4b  turn 1                       9.1 s    6,419 tokens
├─ intent_parsing                                                      1.3 s      432 tokens
│  └─ ChatGoogleGenerativeAI  [llm]                                    1.3 s      432 tokens
├─ apply_preferences                                                   0.0 s
├─ run_flight                                                          0.0 s
│  └─ flight_agent                                                     0.0 s
│     ├─ intent_parsing                                                0.0 s
│     │  └─ router                                                     0.0 s
│     └─ search_flights                                                0.0 s
│        └─ mcp:search_flights  [tool]                                 0.0 s
├─ budget_decision                                                     0.0 s
│  └─ route_after_budget_decision                                      0.0 s
├─ hotel_activities                                                    1.5 s      884 tokens
│  ├─ hotel_agent                                                      0.1 s
│  │  ├─ intent_parsing                                                0.1 s
│  │  │  └─ router                                                     0.0 s
│  │  └─ search_hotels                                                 0.0 s
│  │     └─ mcp:search_hotels  [tool]                                  0.0 s
│  ├─ activities_agent                                                 0.1 s
│  │  ├─ intent_parsing                                                0.1 s
│  │  │  └─ router                                                     0.0 s
│  │  └─ get_attractions                                               0.0 s
│  │     └─ mcp:get_attractions  [tool]                                0.0 s
│  ├─ destination_intelligence                                         1.5 s      884 tokens
│  │  └─ ChatGroq  [llm]                                               1.4 s      884 tokens
│  └─ route_after_search                                               0.0 s
├─ build_itinerary                                                     5.2 s    4,309 tokens
│  └─ ChatGroq  [llm]                                                  5.2 s    4,309 tokens
├─ evaluate                                                            0.0 s
│  └─ route_after_evaluator                                            0.0 s
├─ persist                                                             0.0 s
├─ merge                                                               0.0 s
└─ extract_preferences                                                 1.0 s      794 tokens
   └─ ChatGroq  [llm]                                                  1.0 s      794 tokens
```

34 spans; every one carried the trip's id and the user's. (The searches took no time because the trip's caches
had been warmed when it was created — Phase 24.) And the same run in the database, from `GET /trips/{id}/runs`:

```
intent_parsing            completed     1296 ms     432 tokens  $0.000310
preferences               completed        1 ms       —
flight_agent              completed       21 ms       —
budget_decision           completed        0 ms       —
hotel_agent               completed       71 ms       —
activities_agent          completed       70 ms       —
destination_intelligence  completed     1477 ms     884 tokens  $0.000396
itinerary_builder         completed     5183 ms   4,309 tokens  $0.001954
evaluator                 completed        0 ms       —
persist                   completed        7 ms       —
preference_extractor      completed     1006 ms     794 tokens  $0.000242
orchestrator              completed     9136 ms       —
                                                    6,419 tokens  $0.002902
```

**The trace and the rows agree to the token: 6,419 and 6,419.** They are counted by two different mechanisms —
LangChain's tracer and this project's recorder — so that is a check of both.

**A change** — "Could we stay somewhere nicer? A better hotel please." — two more traces under the same trip, turn 2:

```
classify refinement   trip 45a86c91  user 8ce59c4b  turn 2             1.2 s      524 tokens
└─ ChatGoogleGenerativeAI  [llm]                                       1.1 s      524 tokens

refine trip: targeted_hotel   trip 45a86c91  user 8ce59c4b  turn 2     6.1 s    4,712 tokens
├─ hotel_agent                                                         0.0 s
│  ├─ intent_parsing                                                   0.0 s
│  │  └─ router                                                        0.0 s
│  └─ search_hotels                                                    0.0 s
│     └─ mcp:search_hotels  [tool]                                     0.0 s
├─ build_itinerary                                                     6.1 s    4,712 tokens
│  └─ ChatGroq  [llm]                                                  6.0 s    4,712 tokens
├─ evaluate                                                            0.0 s
├─ persist                                                             0.0 s
└─ merge                                                               0.0 s
```

Rows of that turn: `refinement_classifier` 524 tokens $0.000245, `itinerary_builder` 4,712 tokens $0.002084 —
5,236 tokens, $0.002330. `GET /traces?trip_id=45a86c91…` on the receiver returned exactly these three traces
(34, 2 and 11 spans), none missing the trip's id or the user's on any span.

**A budget too tight** — the same trip for ₹15,000 — is the re-planning branch, live. Escalated after 5.7 s:

```
plan trip   trip a426fbf2  user 8ce59c4b  turn 1                       5.4 s      427 tokens
├─ intent_parsing                                                      1.6 s      427 tokens
│  └─ ChatGoogleGenerativeAI  [llm]                                    1.5 s      427 tokens
├─ apply_preferences                                                   0.0 s
├─ run_flight                                                          0.0 s
│  └─ flight_agent → search_flights → mcp:search_flights  [tool]
├─ budget_decision                                                     0.0 s
├─ run_flight                                                          3.8 s
│  └─ re-plan 1: cheaper flights                                       3.8 s
│     └─ flight_agent → search_flights → mcp:search_flights  [tool]    3.8 s      ← a real search, one more stop allowed
├─ budget_decision                                                     0.0 s
├─ run_flight                                                          0.0 s
│  └─ re-plan 2: cheaper flights                                       0.0 s
│     └─ flight_agent → search_flights → mcp:search_flights  [tool]    0.0 s
├─ budget_decision                                                     0.0 s
└─ escalate                                                            0.0 s
```

(Abridged: the agent's inner steps are drawn on one line.) That trace showed something wrong — see #1 under
"Found and fixed". After the fix, the same run again:

```
│           └─ mcp:search_flights  [tool]                              0.0 s   ✗ NO_RESULTS: No flights from DEL to JAI within ₹8,250.
```

**What left the machine.** Everything the backend had sent by then — 78 runs, 504,527 characters of JSON, read
back from the receiver's `/records` — was searched:

| Looked for | Found |
|---|---|
| the value of each secret in `.env` — the Groq, Google, Duffel, LiteAPI, OpenTripMap and OpenWeather keys, the JWT secret, the Postgres password, the database and Redis URLs (eleven values) | none |
| an email address | none |
| a bearer token or a JWT | none |
| `db`, `publish_fn`, `on_complete` among any run's inputs | none |
| the text "api_key" | yes — as LangChain's placeholder, which names the variable and not its value: `"groq_api_key": {"lc": 1, "type": "secret", "id": ["GROQ_API_KEY"]}` |

What *is* in it is the trip: destination, dates, budget, interests, the prompts, the plan (#177).

**A LangSmith that cannot be reached.** The backend started with `LANGCHAIN_ENDPOINT=http://localhost:9` —
an address nothing listens on — and a made-up key; the same Jaipur trip planned:

| | |
|---|---|
| the plan | completed in 9.2 s, as ever — the upload is a background thread's business |
| the library's log | `Failed to multipart ingest runs: Connection error caused failure to POST http://localhost:9/runs/multipart in LangSmith API. Please confirm your LANGCHAIN_ENDPOINT.` |
| stopping the backend | 5.3 s after it was asked to, with `[TRACING] the last traces were not sent within 5 s` |

That last line is the bound doing its work: the library was still retrying, and without the bound the process
would have waited for it. A LangSmith that is down costs a deploy five seconds, and no traveller anything.

**The switch without a key** logs `[TRACING] off: LANGCHAIN_TRACING_V2 is true but LANGCHAIN_API_KEY is empty.
Set a key in .env, or switch tracing off.` and plans as ever.

## Dev B — the tokens and what they cost

**The columns.** Migration 006 adds `agent_runs.token_count` and `agent_runs.cost_usd`, both nullable. A row for
work that asked no model has neither — `NULL`, not 0.

**Where the numbers come from.** Every reply of a LangChain chat model carries its token counts. One callback
handler, attached to every model call in the process by LangChain's configure hook, reads them and credits them to
the meter that is open where the model was asked — the innermost one (`src/ai/usage.py`). `timed_run()`, which every
agent already used for its row's duration, opens that meter. The stock `get_usage_metadata_callback` was tried first
and counted every token twice — once for the agent and once for the orchestrator around it (#180).

**The price table** (USD per million tokens, input / output):

| Model | Used for | Price |
|---|---|---|
| `openai/gpt-oss-120b` (Groq) | the itinerary, the local tips, the preferences | 0.15 / 0.75 |
| `openai/gpt-oss-20b` (Groq) | the fallback | 0.10 / 0.50 |
| `gemini-3.5-flash-lite` | reading the request; classifying a change | 0.30 / 2.50 |
| `gemini-3.5-flash` — the code's default `GEMINI_MODEL` | the same, when `.env` does not name another | **none on record**: tokens counted, cost left out |

Taken on 2026-10-07 from price trackers that quote the providers. **The providers' own pages were not read** — the
two page fetches that would have confirmed them were declined during the build. `LLM_PRICES` in `.env` corrects or
adds a price without a code change.

**`GET /admin/cost-summary`** on the live database after the runs above:

```json
{ "since": "2026-09-11",
  "total":    { "runs": 8, "tokens": 12499, "cost_usd": 0.005802, "unpriced_tokens": 0 },
  "by_agent": [
    { "agent": "itinerary_builder",        "runs": 2, "tokens": 9021, "cost_usd": 0.004039, "unpriced_tokens": 0 },
    { "agent": "intent_parsing",           "runs": 3, "tokens": 1276, "cost_usd": 0.00088,  "unpriced_tokens": 0 },
    { "agent": "destination_intelligence", "runs": 1, "tokens": 884,  "cost_usd": 0.000396, "unpriced_tokens": 0 },
    { "agent": "refinement_classifier",    "runs": 1, "tokens": 524,  "cost_usd": 0.000245, "unpriced_tokens": 0 },
    { "agent": "preference_extractor",     "runs": 1, "tokens": 794,  "cost_usd": 0.000242, "unpriced_tokens": 0 } ],
  "by_date":  [ { "date": "2026-10-11", "runs": 8, "tokens": 12499, "cost_usd": 0.005802, "unpriced_tokens": 0 } ] }
```

By hand: 6,419 (the plan) + 5,236 (the change) + 427 + 417 (the two tight-budget runs) = 12,499. ✓

**The alert.** After a row with a cost is written, the trip's rows are added up, and the row that takes the total
past `LLM_COST_ALERT_USD` logs it — once:

```
[COST ALERT] trip 6a0f…: the models have cost $0.1224 over 2 runs — past the $0.10 one trip is expected to stay under
```

The roadmap's test is "fires in a test simulating 10 evaluator retries". What ten retries cost depends on the model:

| | one build of the plan (2,400 in, 3,600 out) | ten retries | the alert at $0.10 |
|---|---|---|---|
| at a frontier model's price, $3 / $15 a million | $0.0612 | $0.612 | fires once — on the second |
| at `gpt-oss-120b`'s price, $0.15 / $0.75 | $0.00306 | $0.0306 | does not fire |

Both are tests. On the live run a whole plan cost $0.0029, so $0.10 is some thirty-four plans; and the evaluator's
loop stops at three retries a turn. The default is the roadmap's; `LLM_COST_ALERT_USD=0.02` is what would catch a
runaway on these models, and the second test shows it does (#184).

Live, with the real backend started with `LLM_COST_ALERT_USD=0.002` and one trip planned (Udaipur, 6,536 tokens,
$0.002993), the log had the warning once — on the builder's row, the third with a cost, which took the trip from
$0.000684 to $0.002993 — and not again when the preference extractor's row was written after it:

```
[COST ALERT] trip 528f9612-8ad6-4e9c-b5f2-944ee9c255df: the models have cost $0.0030 over 3 runs — past the $0.002 one trip is expected to stay under
```

## Found and fixed along the way

1. **A tool that failed looked like one that worked.** In the first live trace of the tight budget, the search that
   explains the whole outcome — "no flights within ₹8,250" — was a span like any other: the MCP client returns an
   error instead of raising, so nothing told the span. It is now ended with the error, and shows as failed (#185).
2. **Reading what a change asks for cost tokens that were written nowhere.** The classifier runs in the route, before
   any agent, so no row had its tokens and every total would have been short by them — 524 tokens on the live change,
   a tenth of the turn. It now has a row, `refinement_classifier`, and a trace (#182).
3. **The stock usage callback counted every token twice** wherever one timer was open inside another — which is
   every agent inside every run. Hence one recorder and the innermost-meter rule (#180).
4. **The first probe against a stand-in server hung for ever.** Its `/info` reply lacked `size_limit`; the library's
   sending thread died on the missing key, silently, and `flush()` then waited on a queue nobody was emptying. The
   stand-in answers in full now (and a test holds it to that), and the app's own shutdown waits five seconds at
   most — a real LangSmith having a bad day must not be able to hang a deploy (#178).
5. **`.env` alone would not have switched tracing on** under `uvicorn` (#173).
6. **LangChain's ready-made fake chat model loses its token counts when it is streamed**, so the tests would have
   said nothing about streamed replies. `tests/fakes.py` has its own, which reports usage on the last piece as the
   real providers do.
7. **Each phase's version test broke the one before.** One test now, against the README's status line (#186).
8. **The alert said "past the $0.00".** On the live run with the limit at $0.002 the message rounded the limit to
   cents. A limit is now printed as it was set (`$0.10`, `$0.002`), and one run is "1 run".
9. **`langsmith` was not in `requirements.txt`.** It was installed only because LangChain depends on it; the app
   now imports it directly, so it is declared.
10. **A refinement was refused when its classification could not be logged — the opposite of what the code said.**
    The route catches a failed write of the classifier's row "so that a refinement is not refused over
    bookkeeping". Breaking that `except` on purpose changed no test: none went down that road. The test written for
    it failed on the *unbroken* code with a 500 — the rollback after the failed write expires the trip, and the
    run was then started on an object that could no longer be read. The trip is reloaded after the rollback (#182).
11. **The cost tests read this machine's `.env`.** Following this phase's own instructions — lower
    `LLM_COST_ALERT_USD` to see the alert, correct a price with `LLM_PRICES` — would have failed them. A fixture
    gives every test the limit and the prices the code ships with (#187).
12. **A comment claimed more than the code did.** `run_inline = True` on the recorder was explained as what lets it
    find the right meter. Breaking it on purpose was planned, and reading LangChain first showed the break would
    change nothing: a handler that is not run inline is run in a thread with a copy of the caller's context, and
    finds the same meter. The flag only saves that trip through a thread pool, and the comment now says so.

## Tests

**897 unit + contract, 51 integration, 60 browser — all passing.** New in this phase: 43 unit, 8 integration.
Nothing in them reaches a network: the models are stand-ins that answer through LangChain with token counts
(so the callback that counts them is the one that runs on the real stack), and LangSmith is a stand-in that keeps
what the real client sends it.

| File | Tests | What they hold |
|---|---|---|
| `tests/unit/test_phase26_tracing.py` | 21 | **The trace**: one per run with a span for every step; a model call with its tokens, and every step above it showing the sum; trip and user on every span; filtering by trip; the re-plan branch; numbered evaluator retries; a refinement, a retried search and "add a day" as traces with the graph's steps, each step with what went in and came out; a tool call as a span — and not a trace when it belongs to no run; a failed tool as a failed span. **What leaves the machine**: no live objects and the key only in a header — in JSON batches, in multipart, and in the compressed multipart a current LangSmith is sent. **On and off**: off by default, off without a key, off in the test stacks, not switchable from a shell's environment; off sends nothing, also to a client that was in use a moment ago; the app configures it at startup and flushes at shutdown, also when something else fails to close; a service that is down never fails a plan; shutdown waits five seconds, not for ever. **The reader**: records merged and hung under their parents; the stand-in answers `/info` in full |
| `tests/unit/test_phase26_costs.py` | 21 | **Prices**: per model, input and output apart; a dated or prefixed name priced as the longest model it starts with; `LLM_PRICES` adds and corrects; a model without a price counted and not costed, warned about once; no model asked is `None`, not 0. **The meter**: the innermost one only; side-by-side work counted apart; nothing credited to a meter that has closed; a streamed reply once; a silent provider and a failed call claim nothing. **The rows**: tokens and cost on a logged run; the builder's row to the token; each agent its own, a search none. **The alert**: ten retries at a frontier price fire it once; at the real prices they cost three cents and do not; a limit under a cent is printed as set; one trip only, off at 0, never fails a run. **The route**: 401 without a token, 422 for a date that is not one |
| `tests/unit/test_version.py` | 1 | the app's version is the phase the README says |
| `tests/integration/test_phase26_tracing_integration.py` | 8 | Through the HTTP API on Postgres and Redis: **a plan's rows and its trace agree to the token**, and the timeline and `GET /admin/cost-summary` show the same figures (by agent dearest first, by day, `since`); a refinement's classifier on a row of its own, and three traces under one trip while another trip's stay apart; a refinement goes ahead when that row cannot be written; a way out of a budget conflict is a trace named for it; ten retries against Postgres' own `SUM`, with two other trips in the table; tokens that could not be priced; migration 006 down and up; the local receiver over real sockets, with `/traces?trip_id=` and `/records` |

Two existing integration tests were updated: a refinement's rows now begin with `refinement_classifier`.

The frontend gained two optional fields on a type and no page: `tsc`, the linter and all 60 browser tests were
rerun and pass, and there is nothing new for a browser test to look at.

What was run on the final code, 2026-10-11:

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check` | clean |
| `pytest tests/unit tests/contract` | 897 passed |
| `RUN_INTEGRATION=1 pytest tests/integration` | 51 passed |
| `npx playwright test` | 60 passed |
| `npx tsc --noEmit`, `npm run lint`, `next build` | clean; the production build completes |
| CI's own steps in a clean `python:3.11-slim` container, with no `.env` | lint clean, 897 passed — on freshly installed `langsmith` 0.14.7, `langchain-core` 1.6.9 and `langgraph` 1.2.14, newer than the 0.10.17 / 1.6.3 / 1.2.10 this was written on |
| `docker compose build backend` | builds; inside the image tracing switches on with the switch and a key, and stays off without the key |
| `alembic check` | no drift between the models and the migrations; the dev database is at 006 |
| the live run above | the real backend, real models, the local receiver |

### Break it on purpose

Eighty-one breaks, each applied to a copy of the repo, one line at a time, and the tests run against it —
the whole unit and contract suites and this phase's integration tests, on a database of the copy's own. **All
eighty-one are caught**, none by a hang. They were not at first, and what the misses showed is the useful part:

| Break that got through | What it showed | What was done |
|---|---|---|
| A dated name is priced as the *shortest* model it starts with | the test only used a name that matched exactly | a versioned name of each of two overlapping models is priced |
| A meter stays open after its block | every nested meter in the tests sat in its own graph node, which hides it | one task, three meters, a call between and after them |
| Off is not said to the tracing library | the test began after a fixture had already switched the library off | the test first puts the library in the state a fresh process is in — where the shell's variables do switch it on |
| A node called outside the graph records no output | only the names of those steps were checked | what went into a step and what came out is |
| **A classification that cannot be logged refuses the refinement** | nothing went down that road — and the road was broken: the test written for it failed on the *unbroken* code with a 500 | the bug was fixed (#182), and the fix is itself a break that is caught |
| A re-plan's trace is called a plan | `/replan` was not traced in any test | a budget conflict and the way out of it, as two traces |
| A later record of a run wipes what the earlier one said | no record in the test said `None` for anything | the record of a run's end now does |
| The receiver loses a run sent on its own | untested — and the first test for it changed nothing, because the library's client repeats the id in what it sends | the request is also made as the API allows it, with the id in the address only |
| The app never configures tracing / never sends the last traces / tracing is on by default | **reported as caught, and were not** — see below | a test of the app's own startup and shutdown; the shipped defaults |

**The run that lied.** The first pass reported 67 of 73 caught. Seven of those were wrong. One break makes
migration 006's downgrade leave a column behind; the migration test caught it — and then could not put the schema
back, because the upgrade failed on the column that was still there. The test database, shared by every run that
follows, stayed half-migrated, and every later break that reached the integration tests "failed a test": the first
one that touched the database. What gave it away was one test catching breaks in files it never uses — the local
receiver's, the app's startup. Five of the seven were in fact covered by nothing: the three in the table's last
row, and the two before it, which the second pass then reported honestly as misses. Since then the migration test
removes whatever a downgrade left before it upgrades again (#187); the break-on-purpose run has a database of its
own and checks it after every break; and the whole set was run again from the start on the final code — 79 of 81
caught, then 81 of 81 once the last two had their tests.

| Break | Caught by |
|---|---|
| Input and output prices swapped / per thousand, not per million / an unpriced model costs $0 / no model asked reads as 0 tokens / the `models/` prefix is kept / `LLM_PRICES` is ignored / an unpriced model is warned about on every call | the price tests |
| The recorder is never attached / the reply's model name is ignored / output tokens read from the total / a reply without usage counts as 0 | the meter's tests |
| `timed_run` hands out a meter nothing is credited to / a row never gets its tokens / never gets its cost / `GET /trips/{id}/runs` does not return them | a logged run carries its tokens and their cost |
| The builder's, the tips agent's, the preference extractor's, the classifier's, the orchestrator's own rows get no tokens | each agent's row; the plan and the refinement over HTTP |
| The alert never fires / fires on every row past the limit / a limit of 0 does not switch it off / a failed sum fails the caller / the limit is $1 / a limit is printed without its cents / one run is "1 runs" | the alert's tests |
| The alert adds up every trip, not this one | integration: ten retries, with two other trips in the table |
| Live objects are uploaded with a run's inputs / with its outputs | what is uploaded has no live objects |
| The test stacks trace / tracing switches on without a key / switching it off leaves the library tracing | on, off, and what cannot switch it |
| No `trip_id` / no `user_id` / no trip tag on a trace / a plan's trace is not tagged at all / a refinement's trace has no metadata / traces go to the library's default project | a span for every step; tagged with its trip and its user; filtering by trip |
| A re-plan is no span of its own / does not carry its attempt | the re-planning branch |
| Evaluator retries are not numbered / do not say what failed | numbered spans |
| A node called outside the graph has no name of its own / a trace's name does not say which change it is / a retried search is called a refinement | a refinement and a retried search are traces of their own |
| A tool call outside any run is a trace of its own / a failed tool call is not a failed span | the tool spans |
| The flight, hotel, activities agent's graph is an unnamed span / the tips agent is no span of its own | a span for every step (the tree is compared name by name) |
| Shutdown waits for ever | shutdown waits, but not for ever — the test's stuck flush gives up after 30 s, and the warning that should have been logged is not there |
| The last traces are not sent when something else fails to close | the app's startup and shutdown |
| `since` is ignored / runs that asked no model are counted / unpriced tokens are the priced ones / agents cheapest first / days newest first / the default is today only | integration: the summary against the rows it adds up |
| The summary needs no login | 401 without a token |
| The timeline shows no tokens / the classifier is no trace / the trip is not reloaded after a failed write | integration, over HTTP |
| The migration's downgrade leaves a column behind | integration: 006 down and up — and the database is whole afterwards |
| A span's tokens leave out the spans inside it / filtering by trip returns every trace / the stand-in's `/info` lacks a key the library reads | the reader's tests (the last one slowly: the library's sending thread has died, and the test's own bounded flush reports that nothing was sent) |
| The receiver ignores `?trip_id=` / `/records` returns nothing | integration: the receiver over real sockets |

One break was planned and not made: `run_inline` on the recorder. Reading LangChain first showed it would change
nothing (#12 under "Found and fixed") — the comment that claimed otherwise was the defect.

## Done criterion checklist

| Roadmap | Status |
|---|---|
| `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_API_KEY` in `.env` | ✅ both settings, read by the app and passed to the library (#173); `LANGCHAIN_PROJECT` and `LANGCHAIN_ENDPOINT` beside them |
| Every LangGraph node execution is a span: input, output, duration, token count | ✅ on the uploaded records, in tests and on a live run |
| Every trace tagged with `trip_id` and `user_id` as metadata | ✅ on every span of every trace — plan, re-plan, refinement, retried search, classifier |
| Re-planning branches appear as distinct spans | ✅ "re-plan N: cheaper flights" — in a test, and live with a real second search |
| EvaluatorAgent retry loops appear as numbered spans | ✅ "evaluator retry N" — in a test. Not seen live: the real model's plans passed the evaluator on every run |
| **Acceptance A:** a complete trace **in LangSmith**; re-plan span; numbered retries; filtering by `trip_id` | ⚠️ **all four on what is uploaded** (both encodings, and against a real HTTP server on a real run) — **not seen in LangSmith's UI**: no key in this build. The step is below |
| `agent_runs.token_count`, `cost_usd` (Alembic migration) | ✅ migration 006, up and down |
| Token usage extracted from a LangChain callback and written to the DB | ✅ one recorder; each row its own agent's tokens; live rows and live trace agree at 6,419 |
| `GET /admin/cost-summary?since=` — tokens and USD by agent and by date | ✅ checked by hand against the live rows (12,499 tokens) |
| Single trip over $0.10 → log warning | ✅ once, on the row that crosses it; seen live with the limit lowered |
| **Acceptance B:** `token_count` populated after each run; the summary correct; the alert fires in a test simulating 10 evaluator retries | ✅ — the last at a frontier model's price; at this app's prices ten retries cost three cents, and a second test says so (#184) |

## Known limits

- **Not seen in LangSmith.** No key. What is uploaded was checked on all three of the library's roads, and against
  a real HTTP server; that LangSmith accepts the key, draws this tree and filters on `trip_id` in its UI was not.
  See the last section.
- **A LangSmith that is down adds five seconds to every shutdown**, and its traces are lost — nothing queues them
  to disk for later.
- **The tracing library writes a shortened key into its own error line.** When it cannot reach the server its log
  message ends `API Key: lsv2_…` — the key's first five characters, asterisks, and its last two. That is the
  library's doing, not this project's; this project logs the key nowhere. Worth knowing where logs are shipped on.
- **The prices were not read from the providers.** They are right as far as two price trackers are; `LLM_PRICES`
  overrides them. `gemini-3.5-flash` has no price at all, so a deployment on the code's default Gemini model has its
  intent-parsing and classifier tokens counted and their cost missing — the summary's `unpriced_tokens` says how many.
- **`cost_usd` is a list price, not a bill.** On the free tiers this project runs on, nothing is charged. Cached-input
  discounts and batch prices are not modelled.
- **Embeddings are not counted or traced.** An itinerary is embedded by a direct HTTPS call to Gemini's
  `gemini-embedding-001` (two calls for each saved plan, one for each search typed) — not through LangChain, so
  the recorder never sees it, and not as an agent run, so there is no row to put it on. Those tokens are bought
  and are in no total here.
- **A trace uploads the trip.** Destination, dates, budget, the travellers' names and interests, the prompts, the
  plan. No keys, no email address, no live objects — but it is personal data going to a third party, and switching
  tracing on is a decision about that.
- **`/admin/cost-summary` has no admin check**, like `/admin/embedding-health`: any signed-in user can read the
  system's totals (no trip's data). The admin role comes with the dashboard phase.
- **The alert is a log line.** Nothing pages anyone, and nothing stops a trip that has passed the limit.
- **The summary's days are UTC days**, as `created_at` is stored.
- **No page shows any of this.** `GET /trips/{id}/runs` and the timeline carry the tokens; the admin dashboard phase
  is where they get a screen. The browser tests therefore have nothing new to cover; all 60 were rerun and pass.
- **The receiver is a development tool**: it keeps what it is sent in memory, checks no key, and listens on
  127.0.0.1 only.

## The one step that needs a key

1. Create a key at smith.langchain.com (Settings → API keys).
2. In `.env`: `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY=<the key>`. (A workspace in LangSmith's EU
   region also needs `LANGCHAIN_ENDPOINT` set to the EU address LangSmith gives for it.)
3. Start the backend — the log must say `[TRACING] LangSmith tracing is on — project 'tripplanner-ai', sent to LangSmith`.
4. Plan a trip. In LangSmith, project **tripplanner-ai**, there is one trace named "plan trip". Its tree should
   match the one above.
5. Filter the project's runs on the metadata key `trip_id` with the trip's id as its value. Only that trip's
   traces should remain.
6. Plan one with a budget only just too small for its flights (the Jaipur trip above at ₹15,000) to see
   "re-plan 1: cheaper flights".

If step 3 says "on" and nothing appears, the backend log has the library's own error (a refused key is a 401 there).
