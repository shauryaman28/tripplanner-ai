# Phase 33 — Eval Suite

**Status: built, asked and measured on real providers; not yet graded.** The suite, its runner and its reporting are done and tested; all fifteen scenarios were asked of the healthy build and of a build broken on purpose, on real models and providers, and every record is kept. The grading itself — Claude Haiku 4.5 — waits for one thing only the user can give it: `ANTHROPIC_API_KEY` in `.env`. The records can then be graded without asking the planner again (`python scripts/run_eval.py --regrade docs/evals/baseline --label baseline --assert-min-score 3.5`).

**What the phase delivers (roadmap):** Eval suite runs automatically, produces a score table, and catches a regression when run against an intentionally broken version.

How to run it: `HOW_TO_RUN.md`, Step 31. Decisions: `DECISIONS.md` #284–#296. The results: `docs/eval-results-<date>-*.md`, one line per run in `docs/eval-history.csv`, and every record a run kept in `docs/evals/`.

## What was built

```
evals/scenarios.py          ← the fifteen trips: what each asks, and how it should end
evals/driver.py             ← asks a backend for them over its API, as a traveller would; keeps everything it served.
                              serve(): this checkout's backend on a spare port, against a database of its own.
                              Pacer: plans kept apart for the model provider's per-minute quota. Awake: was it slept through?
evals/measure.py            ← what can be measured exactly, for each criterion — and the ceiling each problem sets
evals/grader.py             ← Claude Haiku 4.5: the request, the searches, the plan and the measurements → 0–5 and reasons
evals/report.py             ← a criterion is the grader's score held to the ceiling; a scenario is its lowest criterion;
                              the table, the history line, the JUnit file, the verdict
scripts/run_eval.py         ← the command: --assert-min-score, --plan-only, --regrade, --only, --api, --junit, --no-history

src/ai/mcp_server/models.py + tools.py     ← found by the eval: a flight search with nothing within the budget says
src/ai/agents/budget_decision.py              what the cheapest flight costs, and the ways out are priced from it
.github/workflows/nightly.yml              ← the suite every night, skipped until its keys are repository secrets
tests/unit/test_phase33_eval.py            ← NEW
tests/fixtures/evals/                      ← six real records: what the API served for a scenario
docs/evals/                                ← the runs kept: the healthy build, and the build broken on purpose
```

## Dev A — the scenarios and the grader

**Fifteen scenarios** in the roadmap's mix: three happy paths (Jaipur, Goa, Varanasi), two budget conflicts (Kochi on ₹12,000 for two, Udaipur on ₹15,000 for five nights), two ambiguous queries (a trip with nothing said about what the traveller enjoys, which the planner should ask about; "something slow and pretty — we're really not museum people"), two group trips (four friends who like different things; a family of three), four edge cases (₹3,000 for two to Goa; Ziro, a valley with no airport; nine travellers, the most the API takes; a start date in the past) and two refinements (a better hotel; fewer temples, more food). Each says how it should *end* — a plan; a question, then a plan; a budget conflict, then a plan; a refusal; for Ziro, a plan or a reason given — and dates are counted from the day the suite runs, 45 days out, as the sandbox providers want them.

**The grader is Claude Haiku 4.5, held to what code can measure** (#285). The five criteria are the roadmap's. For each, code measures first:

| Criterion | Measured by code | Read by the grader |
|---|---|---|
| tool correctness | the searches made in the graded run; each one's place, dates, travellers and interests against the trip; a hotel budget the trip can pay; a budget choice applied; a change that searched more than it needed | whether the interests searched reflect what was *meant* ("not museum people") |
| budget consistency | `total_cost` against the `estimate_budget` tool itself, at the prices the searches found — within 5%; over the traveller's budget; the travellers' shares; for a conflict, whether "raise the budget" would pay for the cheapest flight found | whether the ways out offered are sound and honestly priced |
| hallucination | every place, hotel and flight, by name, against what the searches returned; every copied price | whether a description, a time or a detail says more than the source did |
| completeness | every date once; each day's slots; a slot left empty while places found went unused; a hotel every night but the last; for a group, something for everyone | whether the plan is thin where it need not be |
| refinement accuracy | how the change was read; what changed — dates, flight, hotel, each activity slot — and what kinds of stop before and after | whether the change did what was asked, in the way asked |

Each problem found sets a ceiling on its criterion (an invented place: 1; a total 12% off: 2; an empty slot beside unused places: 3). The grader is shown the request, how the run ended, every search's parameters and trimmed results, the plan (and the plan before, for a change) and the measurements as facts; it answers through a tool it is made to call, a whole number from 0 to 5 and reasons for each criterion asked (#293). Its score counts, no higher than the ceiling; a scenario scores its lowest criterion (#286), 0 if it did not end as it should, and is not scored if it could not be run (#287).

## Dev B — the runner and the reporting

`python scripts/run_eval.py` starts this checkout's backend on a spare port, against `tripplanner_db_eval` and Redis database 7 (both emptied first), asks the fifteen scenarios over the API — a new account each — and keeps every record in `.evals/<run>/` (`--evidence DIR` to keep it elsewhere): the trip, its status, its itineraries, all its agent runs, every request made, how long it took, whether the machine slept during it. It then grades them, four at a time, and writes:

- `docs/eval-results-<date>[-<label>].md` — the table: scenario, kind, each criterion, the score, why; then each criterion's average and each scenario in detail.
- a line in `docs/eval-history.csv` — the run's average and each criterion's, its verdict, what was not scored, its commit, how long it took (#296).
- with `--junit`, a report the nightly summary reads (#295).

`--assert-min-score 3.5` makes the exit code the verdict: 0 passed, 1 the average below it, 2 a scenario not run or not graded. `--plan-only` asks and keeps, and grades nothing; `--regrade DIR` grades what a run kept, without asking the planner again — which is how one set of plans can be graded twice to see how steady the grader is, and how a run made before the grader's key existed was graded after.

The run is paced for the providers' free tiers (#292): a plan is begun no sooner than 45 seconds after the last one that called the builder, and a scenario a provider refused for quota is asked again once, a minute later.

## Found and fixed along the way

- **"Raise the budget" that led back into the same conflict** — the eval's first finding, in its first, unfinished run (#294). With ₹12,000 for two to Kochi the flight search found nothing within it and said only that; the budget check, knowing no fare, offered 25% more — ₹15,000 — and taken, it stopped on the budget again. The record is kept (`tests/fixtures/evals/conflict-kochi.json`, and a test that says what it shows). The flight search now says what the cheapest flight costs (`ToolError.cheapest_inr`) and the ways out are priced from it. On real providers afterwards: "The cheapest flight costs ₹30,434 — more than the whole budget of ₹12,000"; raising it means ₹61,000; taken, a plan at ₹36,612. For ₹3,000 to Goa every way out is priced from the real fare (₹24,084) and says it is over budget, where it had offered ₹3,750.
- **A run slept through.** The first full run was begun with the lid closed: the machine slept in fifteen-minute cycles and woke for two, and the run went on in the gaps. Nothing failed, but a plan slept through can fail for that alone and its time is not a time. Every record now says how long the machine slept during it (the wall clock against the steady one, as in Phase 30); a wrong ending slept through is not scored, a right one is, and a run's time is reported as not holding. The run was stopped, its five finished records kept as test data, and run again awake.
- **The test of the wait for a run** first ended the fake's run as soon as it was asked about, so a driver that stopped waiting too early still found it finished — the break-on-purpose check that tried exactly that survived. The fake now ends the run only when asked a second time.

## The order things were done in

Times are from `date` in the command that ran each step, and from the runs' own records (`run.json`, in UTC there).

| When (IST) | What |
|---|---|
| 14 Oct 08:33 | Begun. `ANTHROPIC_API_KEY` found empty in `.env`; asked for |
| 14 Oct 09:48 | The driver's first real run: the two scenarios that never reach the builder |
| 14 Oct 10:04–10:22 | The first full run, plans only — with the lid closed: the machine slept in fifteen-minute cycles. Stopped; five records kept. **It found the dead-end "raise the budget"** |
| 14 Oct 10:53 | The budget fix, on real providers: Kochi and the ₹3,000 trip asked again |
| 14 Oct 10:59 | The first pass of the break-on-purpose checks: 57 of 65 caught |
| 15 Oct 17:45–17:47 | A healthy run and the second pass at once — **they collided** (below). Both stopped |
| 15 Oct 17:49–17:52 | The checks again, every level isolated: **65 of 65 caught** |
| 15 Oct 17:52–18:01 | **The healthy build, all fifteen** (`docs/evals/baseline/`): 8 min 42 s, awake |
| 15 Oct 18:05 | `group-kochi-family` asked again with its new budget |
| 15 Oct 18:06–18:15 | **The build broken on purpose, all fifteen** (`docs/evals/broken-interests/`): 9 min 28 s, awake |
| 15 Oct 18:17–18:21 | The suites: integration (twice), the browser, Phase 27's production check |

## Live results

### The healthy build (15 October, 17:52–18:01)

All fifteen asked of this checkout (commit `ca9cd19` and the version bump) on real models and providers, in **8 minutes 42
seconds — 6 minutes 20 of it waiting between plans for Groq's per-minute quota** — with the machine awake throughout.
Fourteen ended as they should. The fifteenth was my mistake, not the planner's:

- **`group-kochi-family`, ₹90,000 for three to Kochi in December.** The flights for three came to ₹46,443, two searches for
  cheaper ones found nothing cheaper, and the planner priced the trip as asked (a 4-star stay, peak season) at ₹94,100 —
  so it stopped, and offered a 3-star stay at ₹77,300, a day fewer, or June, each within the budget. That was right. The
  scenario is about a group's plan, not a conflict: it now has ₹1,20,000, and asked again at 18:05 it made a plan. The
  first record is kept (`docs/evals/baseline/group-kochi-family-at-90000.json`).

What code measures in those fifteen records allows an average of at most **4.73**: every criterion of every scenario at 5,
but one.

- **`refine-more-food` — "Fewer temples please — more food and markets instead" — changed nothing.** The plan had no
  temples in it (two forts, two museums, a cinema, four places to eat). "markets" is not an interest the planner knows,
  so the search for it fell back to general sights — in Varanasi, places of worship; the food places it found were the
  ones already in the plan; and the builder, given nothing better, returned the plan as it was and said nothing. To the
  traveller who asked, nothing happened. Refinement accuracy is held to 1. **Found, not fixed:** what "markets" should
  search for, and what a change should say when it finds nothing better, are decisions about the product.

### The build broken on purpose (15 October, 18:06–18:15)

The same tree with one line changed: the attractions search is no longer told what the traveller enjoys
(`"interests": DEFAULT_INTERESTS` in `activities_search_input`). **All fifteen ended exactly as the healthy ones did** —
every plan made, every conflict raised, the question asked, the refusal refused: nothing in how the plans end shows the
bug. What is in them does. The measurements allow the broken build an average of at most **3.07** — nine plans held to 2
for "the attractions search left out what the traveller enjoys" — below the 3.5 the nightly run asserts, before the
grader has read a word. The two vague requests (where what they enjoy is in their own words, not the form) are left to
the grader. Seven of the planner's own unit tests also fail on this copy; its in-run evaluator passes every plan.

### Graded — not yet

The score table, the history's first lines, and the exit codes on these two sets of records need the grader, and the
grader needs `ANTHROPIC_API_KEY`. Nothing else is missing: `--regrade` reads the records and their `run.json` (so the
table reports the asking run's own times) and asks Claude Haiku 4.5 four scenarios at a time — about a minute, and by the
estimate in #293 about ten cents for both.

### Production check

Phase 27's check on the production-like stack — the image built from this tree, version 0.33.0, the budget fix inside
it — with real keys: **25 passed, 0 failed, 1 skipped** (the frontend, not asked for); one real plan in 30.5 s.

## Found and fixed in the eval itself

- **A breakage ran a real eval beside a real eval.** The second break-on-purpose pass and a healthy run were started
  together. One breakage — the key check taken out of `run_eval.py` — sent a unit test into `serve()`: with the scratch
  copy's own `.env` it emptied `tripplanner_db_eval` under the healthy run (whose next request was a 401: its account was
  gone) and started a backend that asked real models for plans. The first pass had done the same a day earlier: the test
  hung until the runner's three-minute timeout, which counts as "caught", and its backend was still running the next day.
  Now a unit test cannot start a backend or empty a database whatever it is broken into doing (it fails at once instead),
  the command stops its backend when it is itself stopped, and every level of the checks has a database of its own. The
  interrupted healthy run's records were thrown away and the run made again.
- **My measurement held a change of activities to the trip's old interests** — "fewer temples" searched without
  "culture", as asked, and was marked a fault. A change to the activities may replace what was asked for; that is now the
  grader's to read.

## Tests

**1,365 unit + contract, 79 integration, 60 browser — all passing** on the tree as committed. New in this phase: 75 unit.

- **`tests/unit/test_phase33_eval.py` (75)** — the scenarios' mix, dates and order; the driver against a fake of the API
  (a plan, a question answered, a conflict and the way out preferred, a refusal in words, a refinement, a run that ends a
  poll late, a quota refusal asked again, the harness failing, the token never written); the pacing and the two clocks;
  every measurement on real records broken on purpose; the grader with a fake of Claude (the tool forced, an answer out of
  range or incomplete never taken, the key from the environment or `.env`); the score held to the ceilings and a scenario
  its lowest criterion; the verdict, the table, the history, the nightly report; the command's exit codes, `--regrade`,
  `--no-history`, a scenario asked again into a kept run; the budget fix, from the flight search to the ways out.
- Changed: `test_phase28_pipeline.py` (the nightly eval step, its keys, its limits), `test_phase24_caching.py` (the flight
  search now says what the cheapest flight costs).
- The integration suite failed once — `test_a_destination_the_planner_cannot_serve_fails_with_the_reason` — while the
  broken build's eval run was going on beside it; that test passed three times alone and the suite twice in full
  afterwards. Not investigated further.

### Break it on purpose

65 deliberate breakages of this phase's code — the scenarios, the driver, the measurements, the grader, the report, the
command, the budget fix and the nightly workflow — each applied to a scratch copy with a database of its own. **First
pass, 57 caught; the last pass, all 65, every one by the eval's own tests within a second.** The eight that survived the
first pass each showed a test that did not test what it said: a model timeout never shown to the quota check; a run that
ends a poll late never waited for; the preferred way out happening to be offered first; an answer with a score of 7, or a
criterion missing, never given twice; the key never passed in the environment; `--no-history` never checked; the "asked
again after a quota refusal" loop with no test at all. And one "caught" was a timeout hiding the collision above.

| Where | Breakages |
|---|---|
| the scenarios (`evals/scenarios.py`) | 3 |
| the driver | 12 |
| the measurements | 18 |
| the grader | 9 |
| the report | 11 |
| the command | 6 |
| the budget fix | 3 |
| the nightly workflow | 3 |

## Done criterion checklist

| | Criterion | |
|---|---|---|
| ✅ | 15 scenarios in the roadmap's mix | `evals/scenarios.py` |
| ✅ | An LLM grader, Claude Haiku 4.5, on the five criteria | `evals/grader.py`, held to `evals/measure.py` |
| ✅ | All 15 scenarios run without crashing | on real providers, twice (healthy and broken) |
| ◻ | Score table produced | the code that writes it is tested; the table of these runs waits for the grader's key |
| ◻ | Intentionally broken version → score drops and is detected | the measurements alone: at most 3.07 against 4.73; the grader's verdict waits for the key |
| ✅ | Suite runs in under 10 minutes | 8 min 42 s asking (on Groq's free tier, paced by its quota) — the grading, about a minute more, waits for the key |
| ✅ | `scripts/run_eval.py` writes `docs/eval-results-{date}.md` | tested; none written yet for a real run |
| ✅ | `--assert-min-score 3.5` exits 0 on a healthy build, 1 on a broken one | tested with a fake grader; ◻ with the real one |
| ✅ | `docs/eval-history.csv` grows correctly after each run | tested; ◻ its first real lines |
| ✅ | Used in the nightly CI of Phase 28 | the step is there; ◻ it needs seven repository secrets |

✅ done and seen · ◻ not yet, and why


## Known limits

- **The grader's steadiness is not measured yet**: grading the same records twice (`--regrade`) will show how much its scores move.
- **The plans are real and so is the grader**: a run costs about 78,000 of Groq's 200,000 free tokens a day and a few cents of Claude. Two full runs a day at most on the free tier.
- **Thirteen plans paced for a per-minute quota are bounded below by it** — about ten minutes on Groq's free tier, whatever the code does.
- **The nightly run needs seven repository secrets** before it does anything but say so. Not seen running on GitHub's machines: Actions cannot be read from a session.
- **With nothing on offer at all, "raise the budget" is still 25% more** — there is no fare to price it from (#294).
- **Fifteen scenarios are fifteen.** India only, as the planner is; sandbox flights and hotels; one run per build. A score moves with the day's fares and the model's mood as well as with the code — the history is for trends, not single points.
- One laptop; no deployment.
