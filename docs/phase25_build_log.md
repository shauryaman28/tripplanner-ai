# Phase 25 — Group Trip Intelligence

**Status: ✅ Complete**
**Done criterion:** A 4-person group trip with varied interests produces an itinerary that balances preferences. Per-person cost breakdown in the output.

Decisions: `DECISIONS.md` #162–#172. Three things are worth knowing before reading on, because none of
them is in the roadmap's entry. The roadmap's own test group cannot be fully served by the attractions
provider: it has no spas, and asking it for one failed the *whole* search (#163). An interest with no
well-known places — food, nearly everywhere — found nothing at all, for any trip (#164). And the model
that writes the plan is asked to balance it, but it is code that makes sure (#167).

## What was built

```
src/ai/group.py                        ← NEW: who a place suits, the group score, taking places in turns,
                                         repairing a plan (rebalance), the summary — pure code, no model
src/ai/agents/activities_agent.py      ← a group is searched member by member, at once; the finds are merged and picked
src/ai/mcp_server/tools.py             ← every attraction says which interest it was found under; lesser-known places
                                         fill up a thin interest; a fair cut; "spa" searched as nothing; adventure is
                                         the outdoors, not stadiums; estimate_budget splits per person
src/ai/mcp_server/models.py            ← Attraction.interests; BudgetInput.group_size; BudgetEstimate.per_person_breakdown
src/ai/builder/builder.py              ← the group in the prompt; rebalance after the model; who each stop is for; the shares
src/ai/agents/evaluator.py             ← a sixth check: unbalanced_group
src/ai/pricing.py, src/ai/itinerary.py ← per_person(): the equal split; its shape (PerPersonBreakdown)
src/ai/orchestrator/orchestrator.py    ← group_members through a plan and every change to it
src/ai/orchestrator/warming.py         ← a group's attraction searches are warmed member by member
migrations/versions/005_trip_group_members.py ← trips.group_members
src/backend/app/models/trip.py, schemas/trip.py, api/routes/trips.py ← GroupMember; what a group must add up to
src/backend/app/pdf/plan.py, document.py, formatting.py ← the share on the cover; "For …" on each stop
src/backend/app/main.py                ← version 0.25.0
prompts/itinerary_builder_v9.md        ← NEW: the prompt record
scripts/group_experiment.py            ← NEW: the roadmap's four travellers against the real attraction search

src/frontend/src/app/trips/page.tsx    ← "Who is going": a name and interests for each traveller
src/frontend/src/components/GroupPanel.tsx ← NEW: who it is for, and how it turned out
src/frontend/src/components/CostSummary.tsx, DayCard.tsx, ItineraryView.tsx ← the share beside the total; "For …" on a stop
src/frontend/src/lib/group.ts          ← NEW: the group summary read forgivingly; the share of an older plan

tests/fakes.py, tests/e2e/stub_backend.py ← a provider that answers by kind and popularity; the roadmap's group; Coorg
```

## The path of a group trip

```
POST /trips  {group_members: [{name, interests}, …]}      → counted, interests put together, saved (migration 005)
   └ cache warming: one attraction search per traveller

activities agent     one get_attractions per traveller, all at once
                     → merged: each place once, with every interest it was found under
                     → suits: [names]; group_score: the share of the group it is for
                     → taken in turns: whoever has the fewest gets the next        (12 places for 4 people, 4 days)

builder              prompt: who travels, who each attraction suits, "at least one for each in every two days"
                     → the model's plan → data scope ✓ → rebalance (code) → budget math ✓
                     → suits on every stop, per_person_cost, per_person_breakdown, group — all written by code

evaluator            + unbalanced_group: a gap that an unused place would fill
```

## The experiment

```
python scripts/group_experiment.py            # needs OPENTRIPMAP_API_KEY; calls no model
```

The roadmap's four travellers — Asha: beach, food; Ben: history, culture; Chitra: adventure; Dev: spa,
relaxation — against the real provider, in five places, for a four-day trip. "Before" is `main`: the only
way to search a group was as one traveller with all seven interests.

| | before: places | of them for Asha / Ben / Chitra | now: places | for Asha / Ben / Chitra / Dev | every two days |
|---|---|---|---|---|---|
| Goa | 8 | 2 / 4 / 1 — a cricket stadium | 12 | 5 / 5 / 4 / 8 | a stop for each |
| Jaipur | 6 | 2 / 3 / 2 — two stadiums | 12 | 5 / 4 / 2 / 4 | a stop for each |
| Manali | 6 | 1 / 1 / 1 | 12 | 4 / 4 / 4 / 3 | a stop for each |
| Kochi | 10 | 4 / 3 / 2 — two stadiums | 12 | 4 / 4 / 4 / 5 | a stop for each |
| Rishikesh | 4 | 0 / 2 / 0 | 12 | 4 / 4 / 4 / 4 | a stop for each |

(Dev is left out of "before": "spa" was a word the old table did not know, so it was searched as the
general sights and nearly everything counted as his. With "wellness" instead of "spa" the whole search
failed: `OTM_ERROR — OpenTripMap API error: HTTP 400`.)

What each traveller's own search could not find, in every one of the five: **spa** — and **beach** in the
three that are inland. It is said on the page, under the traveller's name.

Why the tool had to change before any of this could work — places of popularity 2 and up / of any
popularity, of 10 asked for:

| | Goa | Manali | Rishikesh | Kochi |
|---|---|---|---|---|
| `spas` | HTTP 400 | HTTP 400 | HTTP 400 | HTTP 400 |
| baths, saunas, springs | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| food | 0 / 10 | 1 / 10 | 0 / 10 | 2 / 10 |
| adventure as it was (`amusements,sport`) | 1 / 3 | — | 0 / 2 | 4 / 10 |
| waterfalls | 1 / 3 | 0 / 10 | 0 / 2 | 0 / 0 |
| peaks and caves | 1 / 2 | 4 / 10 | 0 / 4 | 0 / 4 |
| reserves | 2 / 4 | 1 / 5 | 1 / 1 | 1 / 2 |

## Dev A — the multi-objective ActivitiesAgent

- **`group_members`** on the trip and in the orchestrator's state: `[{"name": str, "interests": [str]}]` (#162).
- **One search per member**, concurrent, limit 10 each; merged and de-duplicated by name (#165).
- **`score_activity_for_group(activity, members) -> float`**: the average, over all members, of 1 if the
  place answers one of the member's interests and 0 if not (#166).
- **The pick**: places taken in turns for whoever has the fewest (`pick_for_group`), 10 to 20 of them.
- **The builder**: prompt v9, then `rebalance` — code (#167).
- **The roadmap's "3 unit tests"** are 69, in `tests/unit/test_phase25_group.py`; the three it means are
  `test_the_roadmaps_group_gets_a_stop_for_every_member_in_every_two_days` (run for trips of 2, 4, 5 and 6
  days), `test_the_group_score_is_the_share_of_the_group_a_place_is_for`, and
  `test_a_plan_written_for_one_person_is_repaired`.

### The live run (Goa, 4 days, the real model)

```
POST /trips → 201; group_size 4
[CACHE WARM] attractions: Goa: beach, food — 10 result(s)      [CACHE WARM] attractions: Goa: adventure — 10 result(s)
[CACHE WARM] attractions: Goa: history, culture — 7 result(s)  [CACHE WARM] attractions: Goa: spa, relaxation — 10 result(s)
trip completed after 9.9 s     (itinerary_builder 6.6 s; the three searches 21–87 ms: warmed)

  day 1 morning   Arossim Beach               for Asha, Dev
  day 1 afternoon Goa Chitra museum           for Ben
  day 1 evening   Dudhsagar Falls             for Chitra, Dev
  day 2 morning   Mollem National Park        for Ben, Chitra, Dev
  day 2 afternoon Agonda                      for Asha, Dev
  day 2 evening   Betalbatim Beach            for Asha, Dev
  day 3 morning   Bondla Wildlife Sanctuary   for Ben, Chitra, Dev
  day 3 afternoon Cavelossim Beach            for Asha, Dev
  day 3 evening   Rachol Fort Gate            for Ben
  day 4 morning   Mannawaall Waterfall        for Chitra, Dev
  day 4 afternoon Sugandha                    for Asha            ← a restaurant: food, found among the lesser-known
  day 4 evening   Excavated site in Chandor   for Ben
```

| | days 1–2 | days 3–4 | all |
|---|---|---|---|
| Asha — beach, food | 3 | 2 | 5 |
| Ben — history, culture | 2 | 3 | 5 |
| Chitra — adventure | 2 | 2 | 4 |
| Dev — spa, relaxation | 5 | 3 | 8 — nothing found for: spa |

The model wrote this plan balanced by itself: `rebalance` changed nothing, and the evaluator passed it
first time. Dev's eight are all somebody else's too — a beach is his idea of a rest as well as hers.

## Dev B — the per-person cost

| Where | What |
|---|---|
| `estimate_budget` | `group_size` in; `per_person` = total ÷ travellers; `per_person_breakdown` for more than one |
| `structured_data` | `per_person_cost`, `per_person_breakdown` (flights, stay, activities, total — one traveller's part — and a share per traveller), at the top level |
| the page | "₹16,834 / person · shared equally by 4 travellers" under the total |
| the PDF | "₹16,834 per traveller" under the total on the cover; the cost page's "Per traveller" row |

Live: total ₹67,336.74 → `per_person_cost` 16,834.19; shares 16,835 + 16,834 + 16,834 + 16,834 = 67,337.
`total_cost / group_size − per_person_cost` = 0.00 (the roadmap allows ₹100).

## Found and fixed along the way

| Problem | Fix |
|---|---|
| The interest "wellness" was searched as a kind the provider does not have: HTTP 400, and the whole attraction search failed | Searched as nothing, and said to have found nothing; a test holds the list of kinds the provider knows (#163) |
| "wellness" was a suggestion on the new-trip form | "relaxation" |
| "food" found nothing in three destinations of four: every restaurant is below the popularity the search asked for | Lesser-known places fill up an interest that is short (#164) |
| "adventure" returned cricket stadiums | Peaks, caves, waterfalls, reserves, climbing, the water sports, amusement parks |
| "relaxation" was `beaches,natural` — every cave and peak with it | Beaches, gardens, water, viewpoints |
| Three interests, ten places: four, four, and whatever fitted of the third | One from each interest in turn |
| A place was looked up (a request to the geocoder) before anyone checked there was something to search | The check comes first |
| `estimate_budget`'s `per_person` was the whole total, with a comment promising this phase | Divided |
| `windows()` crashed on a trip with an odd number of days — `pairs[-2] += pairs.pop()` reads the index before the pop and writes after it | Found by its first test; the pop comes first |
| A stop was given up in alphabetical order of its slot ("afternoon" before "evening") | The place that ranks lowest for the group goes |
| A traveller whose only two places were both in the first two days had nothing in the next two, and nothing unused to add | One of them is brought over (`_a_stop_to_bring`) |
| Names in Devanagari were refused: its vowel signs are marks, not letters, and `\w` does not match them | Letters, marks and digits of any script |
| A group plan's attraction searches arrive eleven at once, one more than the provider's ten a second | Nothing to fix: Phase 24's limiter holds the eleventh for a second |
| The page and the PDF preferred the itinerary's saved share and fell back to dividing the total — one number, reached two ways; found because breaking it changed nothing | They divide the total; the branch is gone (#170) |

## Tests

```
pytest tests/unit tests/contract                 847 unit + 8 contract = 855 (69 new)
RUN_INTEGRATION=1 pytest tests/integration       43 (4 new)
npx playwright test                              60 (4 new)
```

`tests/unit/test_phase25_group.py` — the roadmap's group on the real MCP server, agent and builder in front
of a fake provider; who a place suits and the score; the merge, the turns, how many places; the windows; the
repair (an empty slot first, a stop that can be spared, a stop brought over, nine travellers in two days,
nothing when nothing could be better, twice is once); the evaluator's check; the agent's searches and what
it does with an empty or a failed one; the tool's tags, top-up, fair cut, "spa", the kinds the provider
knows; the builder's two prompts, the repair of a model's one-sided plan, what the model may not write;
the split and its shares; the budget tool; what the API lets in; the PDF; warming.

`tests/integration/test_phase25_group_integration.py` — a group trip saved and read back, and refused
when it does not add up; planned over HTTP with a stop for each traveller, their share, and the same on
paper; a change to the plan that keeps everyone in it; migration 005 up and down.

`src/frontend/e2e/group.spec.ts` — the four travellers entered in the form, planned, and on the page: the
share beside the total, "Who it is for", "For Asha and Dev" on a stop, a stop for each in every two days,
"Nothing was found in Coorg for: spa", the same after a reload; what the form refuses; a trip whose
travellers are not told apart; a phone.

### Break it on purpose

Fifty-two breaks, each applied to a copy of the repo, one at a time, and the tests run against it. All are
caught: fifty-one by a test that fails, one by tests that hang. Three did not go that way at first, and each
was worth the finding: two got through because the test's own data could not tell the right behaviour from
the wrong one (the data was changed), and one changed nothing because what it broke was a branch that could
never matter (the branch was removed — the last row of "Found and fixed").

| Break | Caught by |
|---|---|
| "beaches" is not "beach" / the score is a count / a place found twice keeps one search's interests | a place suits…; the group score…; every member's finds become one list |
| The pool is ranked by popularity alone | every member's finds become one list — after its data was changed: the first version could not tell |
| Places are taken for the first member, not the one with the fewest / the agent does not take them in turns | places are taken in turns; the searches run together |
| A place for two counts for one | a place for two counts for both — after a second place of Dev's was added: the first version could not tell |
| An odd last day is checked on its own | a trip is checked two days at a time |
| A full window is never repaired / a stop is never brought over / the total keeps a given-up stop's cost | the repair's own tests |
| A stop is given up though it is someone's only one | the tests hang (the repair no longer ends) and are stopped |
| Any number of places / one traveller is a group / every plan is "balanced" / what was not found is not said | their own tests |
| A name may be anything | a group that does not add up is refused |
| A group is searched as one / a member's search is cut to six / an empty search fails the group / a failed one is taken for empty | the activities agent's tests |
| No top-up / lesser-known first / no tags / the first interests take the cut / "spa" is `spas` again / adventure is stadiums again / the place is looked up first / the old cache key | the attraction search's tests |
| The builder does not repair / keeps the model's `group` / trusts the model's `suits` / does not tell the model who travels / shows a solo trip the new fields / adds no share / gives one traveller a breakdown | the builder's tests |
| The evaluator never fails a group plan / has nobody to retry | the evaluator's check |
| The shares do not add up / the share is the total / the budget tool ignores the travellers | each traveller's share |
| Named travellers are not counted / may share a name / their interests are not the trip's | what the API lets in |
| The travellers are left behind at a plan / forgotten by a change / not passed to the search / what was found is dropped | the orchestrator's tests |
| A group is warmed as one traveller | a group's attractions are warmed member by member |
| The PDF does not say who a stop is for / prints no share | on paper |
| The travellers are not saved with the trip | integration: saved with its travellers |

## Done criterion checklist

- [x] `group_members: list[{"name", "interests"}]` on the trip and in the state
- [x] `get_attractions` once per member's interests; merged, de-duplicated, ranked by average relevance; `score_activity_for_group`
- [x] The builder's day slots balance across the members — asked of the model, made sure of by code
- [x] The 4-person group [beach/food] + [history/culture] + [adventure] + [spa/relaxation]: at least one stop per member per two days; no member's interests fill the plan — in tests, and on the real stack
- [x] `estimate_budget` with `group_size > 1` returns `per_person_breakdown`
- [x] `structured_data` carries the per-person costs at the top level
- [x] The page shows "₹… / person" beside the total
- [x] `total_cost / group_size ≈ per_person_cost` — to the paisa
- [x] Verified on the real stack: the search in five destinations, a plan by the real model, the page, the PDF
- [x] Break-on-purpose checks

## Known limits

- **A spa lover gets no spa.** The provider lists none. The plan says so; it does not substitute.
- **"Balanced" is a stop for each in every two days, not an even split.** A traveller whose interests
  overlap the others' ends up with more stops (Dev's 8 of 12). Nobody is short; nobody is equal either.
- **The provider's own data sets the quality.** It answers nearest-first, not best-first, so a traveller
  who likes history in Jaipur gets the museum and the garden by the centre before Amber Fort; and what it
  calls a place is what it calls it ("City Palace" comes back as a restaurant, because there is one in it).
  Lesser-known places — now used when an interest is short — can be very lesser-known.
- **Supply runs out on long trips.** A plan is made from at most 20 places and uses each once. Four
  travellers for two weeks need 28 stops to have one each in every two days: the rule holds while
  places last, and the summary says where it stopped.
- **More travellers than slots.** Two days have six slots. Nine travellers who each want something
  different cannot all have a stop in them; six do.
- **The cost is split equally.** Not by who a stop is for, who has a room to themselves, or who flew from
  somewhere else.
- **The travellers are fixed when the trip is created.** There is no editing them afterwards, and a
  request in the chat ("my sister likes temples") does not add one.
- **A request cannot take a traveller's last stop away** when another place for them was found: the
  repair runs after every build (#171).
- **A group's attraction search is slower when cold**: up to two requests per interest, more than the
  provider's ten a second for a group of four — the limiter spaces them, about a second in all.
