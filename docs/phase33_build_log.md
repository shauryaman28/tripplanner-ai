# Phase 33 — Eval Suite

**Status:** RESULTS-PENDING

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

RESULTS-SECTIONS-PENDING

## Known limits

- **The grader's own steadiness is measured once**, by grading the same plans twice; it is not a guarantee. Its model takes no temperature here.
- **The plans are real and so is the grader**: a run costs about 78,000 of Groq's 200,000 free tokens a day and a few cents of Claude. Two full runs a day at most on the free tier.
- **Thirteen plans paced for a per-minute quota are bounded below by it** — about ten minutes on Groq's free tier, whatever the code does.
- **The nightly run needs seven repository secrets** before it does anything but say so. Not seen running on GitHub's machines: Actions cannot be read from a session.
- **With nothing on offer at all, "raise the budget" is still 25% more** — there is no fare to price it from (#294).
- **Fifteen scenarios are fifteen.** India only, as the planner is; sandbox flights and hotels; one run per build. A score moves with the day's fares and the model's mood as well as with the code — the history is for trends, not single points.
- One laptop; no deployment.
