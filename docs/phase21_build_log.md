# Phase 21 — Smarter Budget Intelligence

**Status: ✅ Complete**
**Done criterion:** Budget estimates include a seasonal adjustment and a confidence range, and the Goa / ₹40,000 scenario shows three concrete cheaper alternatives.

Decisions: `DECISIONS.md` #125–#131. One departs from the roadmap's wording: the alternatives travel in the
`budget_conflict` event rather than a separate `budget_alternatives` event (#129). Two fix behaviour the
phase brought to light: a trip that fits at typical prices is no longer stopped (#130), and a way out the
traveller picks is gone ahead with instead of being a dead end (#131).

## What was built

```
src/ai/pricing.py                      ← NEW: seasons by destination, typical costs, the estimate arithmetic (pure)
src/ai/mcp_server/models.py, tools.py  ← estimate_budget: destination, month, nights → total_min / total_max,
                                         season, multiplier, off-season months and price
src/ai/agents/evaluator.py             ← the total within the estimate's range; each hotel at the searched price
src/ai/agents/budget_alternatives.py   ← NEW: the trip as asked and three ways out, priced (pure)
src/ai/orchestrator/orchestrator.py    ← the budget check prices a conflict, lets a trip that fits through,
                                         goes ahead with a picked way out; the evaluator gets its range
src/backend/app/api/routes/trips.py    ← POST /replan applies the option as offered (cheaper_hotel, off_peak new);
                                         GET /status returns budget_conflict.estimate
src/backend/app/schemas/trip.py        ← ReplanRequest: two more choices

src/frontend/src/components/TripStages.tsx ← the conflict: the priced trip, three cards with amounts, two actions
src/frontend/src/app/trips/[id]/page.tsx   ← the assistant says the price; the trip refreshes when a way out changes it
src/frontend/src/lib/types.ts              ← ConflictEstimate; the options' amounts; the new choices

tests/e2e/stub_backend.py              ← Jaisalmer: ₹30,000 flights — a conflict whose ways out fit
```

## Dev A — seasons and a range

**`estimate_budget`** takes the destination and the month of travel:

```
estimate_budget(flights=8000, hotels=3000, days=5, daily_spend=2000, destination="Goa", month=12)
→ total ₹33,000 · likely ₹26,400–₹39,600 · peak season (×1.4) · ₹23,571 in the off-season (Jun–Sep)
```

The prices it is given are that month's — a fare found for those dates already carries the season — so
the season sets how wide the range is (±20% peak, ±15% shoulder, ±10% off-peak) and what the same trip
would cost in the destination's cheapest month. Without a month the range is the widest. The seasons
are five profiles of monthly multipliers — coast, heritage, hills, high Himalaya, India-wide — with
about ninety destinations matched by name (#125).

**The evaluator** takes the range as its tolerance: the plan's total, recomputed from the source prices,
must fall within it — a season's ±10–20% where a flat 5% stood. So that a looser band cannot hide a
misquoted hotel, every hotel price in the plan is now checked against what the search found, within ₹50
or 1% (#127):

```
Goa Grand is priced at ₹4,000 a night in the plan, but the hotel search found ₹4,500.
```

## Dev B — a conflict with amounts

When the flights take more than 65% of the budget, the planner stops and asks, as in Phase 10. Now it
prices first — with the real fare and typical costs for the destination and month, no search (#128):

| The roadmap's scenario: Goa, 5 days, ₹40,000, flights ₹28,000, December | |
|---|---|
| The trip as asked (a 4-star stay and things to do) | about ₹56,700 — likely ₹45,400–₹68,000 |
| Stay at a budget hotel | about ₹38,200 · within budget · ₹18,500 less than as asked |
| Make it 2 days instead of 5 | about ₹35,700 · within budget |
| Go in June instead — the monsoon | flights about ₹8,000 less · about ₹40,500 · ₹500 over budget |
| Search for cheaper connecting flights | (not before a re-plan has searched them) |
| Increase total budget to ₹56,000 | what these flights need to pass the check |

The page shows the priced trip in a sentence, the three ways out as cards — each with its total, whether
it fits in words, and what it saves — and the two plain actions as buttons beneath; the assistant's
message says the price too. All of it comes back from `GET /status` after a reload.

**Picking one** applies it as offered: the shorter trip at the length the card said, the off-season on
the dates it said, the budget it named. A shorter trip or a cheaper stay keeps the same flights, so the
check goes ahead with the traveller's choice instead of stopping them again (#131). Before a conflict is
raised at all, a trip that fits at typical prices is let through — the flight-share rule alone stopped
short trips that could afford their flights (#130).

## Found and fixed along the way

| Problem | Fix |
|---|---|
| "Shorten the trip by 2 days" re-planned on the same flights, and the check — which ignores a trip's length — stopped the traveller again: a dead end | The option is applied as offered, and the check goes ahead with a shorter trip or a cheaper stay (#131) |
| "Search for cheaper connecting flights" was offered after a re-plan had already searched connections | Offered only when connections have not been searched |
| The options' savings were made up ("₹flights × 0.35", "~₹8,000–15,000") | Priced from the trip, or left without an amount (#128) |
| A short trip whose flights were 70% of the budget was stopped even when the rest fitted | A trip that fits at typical prices goes ahead (#130) |
| The evaluator's 5% of the total let a ₹10,000 hotel misquote through on a ₹2,00,000 trip, and caught ₹300 on a ₹5,000 one | Each hotel price is checked exactly; the total within the season's range (#127) |
| The integration test's conflict (₹28,000 flights on ₹40,000) comes to exactly ₹40,000 at off-season prices — its outcome would depend on the month the suite ran in | ₹30,000 flights: a conflict in every month |
| The trip's dates on the page stayed as they were until a re-plan finished, though a shorter trip or the off-season had already moved them | The trip is read again as soon as the re-plan starts |

## Tests

- `tests/unit/test_phase21_budget.py` — 43 tests. Seasons: Goa December 1.4 × July, a destination's
  profile, the range by season, the nearest off-season far enough ahead to book. `estimate_budget`: the
  roadmap's example returns a range, without a month the widest, nights, the off-season, a month that is
  not one. The evaluator: a total outside the range is rejected (roadmap acceptance), the range follows
  the season, a misquoted hotel cannot hide inside it, a rounded price is not a misquote. The
  alternatives: the Goa / ₹40,000 conflict with every amount, a 3-star when it fits, no shorter trip when
  none fits, none for a two-day trip or in the off-season, connections not offered twice. The budget
  check: the conflict priced with no agent and no tool called (roadmap acceptance), the event carries the
  priced trip, a trip that fits is not stopped, a picked way out is gone ahead with — but not on flights
  dearer than the budget, no fare means the plain options. The routes: the shorter trip at the length
  offered, the off-season dates, the budget offered, a choice not offered refused, dates that have passed
  refused, a new choice needs a conflict that offered it.
- `tests/contract/test_mcp_contracts.py` — the estimate's range and season fields.
- `tests/unit/test_orchestrator.py`, `test_pipeline_regressions.py` — the Phase 10 conflict now has five
  options; `/status` returns the estimate.
- `tests/integration/test_pipeline_integration.py` — the conflict on the real stack carries priced options
  and the estimate, with no hotel or activities search; a shorter trip picked in a conflict goes ahead on
  the same flights and comes back two days long.
- `src/frontend/e2e/budget.spec.ts` — 3 tests on the stub's Jaisalmer: the priced trip and three cards
  with their amounts, the same after a reload, and the shorter trip planned on the same flights; a cheaper
  stay planned for the whole trip; the off-season moving the trip, where it then fits.
- `src/frontend/e2e/planning.spec.ts`, `polish.spec.ts` — the conflict's cards and actions at both widths.

Behaviours broken on purpose, one at a time — each caught by a test: the check for a trip that fits, a
picked way out stopped again, a picked way out gone ahead with on any flights, the exact hotel price
check, the range ignored, one width for every season, the off-season fare not moved, the cheapest stay
always, connections offered again, the replan ignoring the offered length, the replan taking any choice,
and no lead time to book the off-season.

**616 unit + 8 contract, 21 integration, 37 browser (5 end-to-end flows, 3 budget, 5 polish, 16 draft,
8 change-summary) — all passing.** The unit and contract suites also pass in a clean `python:3.11-slim`
container, `next build` succeeds, and the backend image rebuilds.

Verified on the real stack — backend on :8000 with the live providers and model. `estimate_budget`
through the real MCP server returns the roadmap's example with its range. A Goa trip for two on ₹35,000
found real flights at ₹24,661 — 70% of the budget — and stopped with the trip as asked priced at about
₹46,500 (November, peak) and: a budget hotel ₹34,200 (fits), 2 days instead of 4 ₹32,800 (fits), June
with flights about ₹4,900 less ₹37,200 (₹2,200 over), connecting flights, ₹49,500. Picking the budget
hotel re-planned on the same flights — "going ahead with the cheaper stay you chose" — and the real hotel
search found a stay within what the budget left: the plan came to ₹32,492 of ₹35,000.

## Done criterion checklist

- [x] `estimate_budget` takes the destination and the month: Goa December ≈ 1.4 × Goa July
- [x] A confidence range: `total_min`, `total_max` — ±20% in peak season, by destination and season
- [x] The evaluator rejects an itinerary whose total falls outside the range
- [x] 3+ unit tests for the above (and 40 more)
- [x] On escalation, three concrete alternatives — a 3-star (or budget) hotel, a shorter trip, the off-season — with INR amounts
- [x] They arrive over SSE with the conflict (#129), generated without any MCP call — asserted in a test
- [x] Tested against a known conflict: the Goa / ₹40,000 scenario, the stub's Jaisalmer, a live Goa trip
- [x] DECISIONS.md updated (#125–#131)

## Known limits

- The typical costs are India-wide: a 4-star night is ₹4,500 off-season in Goa and in Varanasi alike. The
  season tells destinations apart; a cost index per destination would be the next refinement.
- The seasons are approximations of each region's tourist calendar, not measured prices; the off-season's
  fare is today's fare scaled by the two months' multipliers. When the traveller picks the off-season, the
  flights are searched again for the new dates and those real prices decide.
- Phase 10's flight-share rule still decides when to stop; the estimate only lets through a trip that
  fits. A trip that would not fit even at a budget hotel, but whose flights are under 65% of the budget,
  is still planned without a conflict.
- A cheaper stay is the traveller's word to go ahead; the hotel search still caps each night at what the
  budget leaves, not at the typical 3-star price the card showed.
- The estimate counts the things to do the plan counts — entry fees and getting to them — and no meals,
  as the plan's own total does not.
- In practice the range check on the total rarely fires on its own: the builder already rejects a total
  that does not add up to its parts within ₹500 (#24), and the misquote check is exact.
