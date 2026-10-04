# Phase 22 — Destination Intelligence Agent

**Status: ✅ Complete**
**Done criterion:** Every itinerary has a "Local Intelligence" section with destination-specific advice that no API can provide.

Decisions: `DECISIONS.md` #132–#139. Prompt: `prompts/destination_intelligence_v1.md`. Three depart from
the roadmap's wording: the agent asks Groq, not Claude Haiku (#132); it runs beside the hotel and
activities searches rather than all three (#133); and the accordion is our own, not shadcn/ui's (#138).
One goes beyond it: the PDF prints the tips too (#139).

## What was built

```
src/ai/agents/destination_intelligence.py ← NEW: the agent — ask, cut the reply down, log one row; no tools
src/ai/itinerary.py                       ← LocalIntelligence (the five shapes) and how much of it is kept
src/ai/builder/builder.py                 ← the tips are attached to the checked draft; the plan's model cannot write them
src/ai/orchestrator/orchestrator.py       ← runs beside the hotel and activities searches; carried by refinements;
                                            fetched with the next change when a plan has none
prompts/destination_intelligence_v1.md    ← NEW: the prompt, why each rule is there, the live runs

src/backend/app/pdf/plan.py, document.py  ← a "Local tips" page after the days
src/backend/app/main.py                   ← version 0.22.0 (it had stayed at 0.19.0 through Phases 20 and 21)

src/frontend/src/lib/tips.ts              ← NEW: reads the tips defensively; marks a place the plan visits (pure)
src/frontend/src/components/LocalTips.tsx ← NEW: the accordion
src/frontend/src/components/ItineraryView.tsx ← the section, under the day cards
src/frontend/src/lib/changes.ts, types.ts ← "Local tips added"; the LocalIntelligence type

tests/fakes.py, tests/conftest.py, tests/e2e/stub_backend.py ← a stand-in model; no real model in any test;
                                            Pondicherry (never any tips), Gokarna (tips with the first change)
```

## Dev A — the agent

**What it is asked.** The trip, as JSON: destination, month, days, travellers, interests. **What it
answers** — from what the model already knows, with no tool to call:

```json
{
  "local_transport": "Auto-rickshaws and app-based cabs are cheapest; for longer stretches, hire a private car with a driver for flexibility.",
  "cultural_norms": ["Dress modestly when visiting temples—cover shoulders and knees.", "…"],
  "tourist_traps": ["Skip the overpriced camel rides near the city gate; opt for a guided heritage walk instead.", "…"],
  "best_times": {"Amber Fort": "Early morning (7-9 am) to beat crowds and enjoy soft light for photos.", "…": "…"},
  "safety_tips": ["November evenings can be chilly; carry a light jacket to avoid sudden cold.", "…"]
}
```

(A live reply, for Jaipur in November.)

**What is kept of the reply.** Strings only, one line each, plain text; a tip at most 240 characters,
a list at most five, six places; every other key dropped (#134). A place the model does not know comes
back `{"unknown": true}` and is no tips at all.

**Where it runs.** Beside the hotel and activities searches, after the budget check (#133):

```
18:39:25.909 → 28.789   flight_agent               2,880 ms
18:39:28.795            budget_decision
18:39:28.803 → 30.315   destination_intelligence   1,512 ms   ┐
18:39:28.801 → 34.019   hotel_agent                5,218 ms   ├ side by side
18:39:28.802 → 34.019   activities_agent           5,217 ms   ┘
18:39:34.029 → 39.136   itinerary_builder          5,107 ms
```

(The same live run, from `agent_runs`.) It added nothing to the 15.9 s the plan took.

**Where its output goes.** `ItineraryBuilder.run` attaches it to the draft once the draft has passed
its checks: `structured_data["local_intelligence"]`. The model that writes the plan never sees it, and
cannot write it.

**When it has nothing** — no key, a rate limit, no answer in 8 s, a reply that is not JSON, an unknown
place — the plan is made without tips and is otherwise whole: `completed`, nothing published, no error
on the page; the agent's row in `agent_runs` says `failed` and why (#135).

**Afterwards.** A change to the plan keeps its tips without asking again. A plan that has none gets
them beside the search its next change repeats (#136).

## Dev B — on the page

**Local tips**, under the day-by-day cards and above the map: an accordion of five sections — Getting
around, Local customs, Tourist traps, Best times to visit, Staying safe — each closed until it is
clicked, with how much is inside ("4 tips") on its heading. It opens with where the advice comes from:
"General advice from the assistant's own knowledge of Jaipur, not from a live source. Prices and timings
change — check locally."

Under **Best times to visit**, a place that is one of the plan's stops is marked "In your plan · Day 1 ·
Evening" and listed first.

When the itinerary has no tips there is **no section at all** — no heading, no placeholder, no error.

The PDF has the same five sections on a **Local tips** page after the days (#139).

## Found and fixed along the way

| Problem | Fix |
|---|---|
| Groq's free tier allows 8,000 tokens a minute per model; the builder alone takes about half | The agent asks at low reasoning effort: about 900 tokens instead of 1,200 (#132) |
| The small model, which has an allowance of its own, reasoned its whole 2,048 tokens away and returned nothing — or, at low effort, put Leh's Stok Palace in Kaza | Not used (#132) |
| Asked "when to go", the model answered with the month of the trip ("Early November, after the monsoon") | The prompt asks for "the time of day to go" |
| The model writes U+2011 hyphens and U+202F spaces ("auto‑rickshaws", "10 %") | Made plain when the reply is cut down |
| Any test that drives the orchestrator would now have asked a real model | An autouse fixture refuses the agent's model in every test; a test that wants tips stubs it |
| The live PDF's tips page was a few lines too long: "Staying safe" sat alone on a page of its own | The page is set closer: five sections of three or four tips fit one page |

## Tests

- `tests/unit/test_phase22_intelligence.py` — 51 tests. What the model is told, and that the trip
  reaches it as data; the call (model, low effort, no retries, the time limit). What is kept: the five
  shapes, markdown and odd characters gone, long tips cut at a word, long lists cut short, nothing
  usable → nothing. Every way of having nothing to say, each with its code; a slow model is not waited
  for. The agent's row; it never raises; **it cannot make an MCP call** (roadmap acceptance). The graph:
  **side by side with the hotel and activities searches** (roadmap acceptance), **the output in
  `structured_data`** (roadmap acceptance), a plan whole without it, a crash costing only the tips, no
  call for a trip the budget check stops. The builder: attached after the checks, the plan's model
  cannot write it, the wrong shape left out. Refinements: kept without asking, fetched when missing,
  an unknown place not asked about again, a day more keeps them, a new trip asks again. The PDF: the
  page and its place, a plan without tips as before, stored tips read forgivingly, text never markup,
  the same headings as the page.
- `tests/integration/test_pipeline_integration.py` — on the real stack: the three agents' `agent_runs`
  rows overlap in time, the tips are in the saved itinerary, no fourth tool call, nothing on the stream,
  a refinement keeps them, the PDF has the page. And: a plan made while the model was down is
  `completed` with no error anywhere, and gets its tips with the next change.
- `src/frontend/e2e/tips.spec.ts` — 10 tests. Six of `lib/tips.ts`: the sections, a place in the plan
  marked and first, nothing usable → no sections, non-text left out, the same place under another name,
  "Local tips added". Four on the page: under the day cards and above the map, all five sections
  closed, a click opens one and closes it, by keyboard too, two open at once, markdown gone, the place
  in the plan marked, still there after a reload and after a change; **no section and no error** when
  the agent had nothing (roadmap acceptance); tips arriving with a change; a phone.

Behaviours broken on purpose, one at a time — each caught by a test: the plan's model writing tips;
tips attached unchecked; asked again on every change; an unknown place asked about again; the three run
one after the other; a crash in the agent failing the plan; a refinement not fetching missing tips;
"add a day" forgetting them; tips not passed to the builder; lists not cut short; markdown kept; long
tips not cut; "unknown" not recognised; no time limit; the trip spliced into the instructions; a failure
logged as a success; no tips page in the PDF; the PDF trusting stored tips; tip text as markup in the
PDF; the sections open by default; the section shown when empty.

**667 unit + 8 contract, 23 integration, 47 browser (5 end-to-end flows, 10 tips, 3 budget, 5 polish,
16 draft, 8 change-summary) — all passing.** The unit and contract suites also pass in a clean
`python:3.11-slim` container, `next build` succeeds, and the backend image rebuilds.

Verified on the real stack — backend on :8000 with the live providers and model, the dev frontend on
:3000: a Jaipur trip planned in 15.9 s with the timings above; its tips are the reply quoted above; two
of the five places (Birla Mandir, Jantar Mantar) were stops in the plan and are marked so on the page;
"a cheaper hotel please" re-ran only the hotel search and kept the tips; the PDF has the Local tips page.
The page was checked at 1440 px and 375 px (no sideways scroll).

## Done criterion checklist

- [x] A fourth agent, from the model's own knowledge — zero MCP tool calls
- [x] The roadmap's output structure: `local_transport`, `cultural_norms`, `tourist_traps`, `best_times`, `safety_tips`
- [x] Wired into the orchestrator's fan-out; its `agent_runs` row overlaps the hotel and activities searches'
- [x] In `structured_data` under `"local_intelligence"`
- [x] 4 unit tests (51)
- [x] "Local Tips" accordion under the day-by-day cards: collapsed by default, expanded on click
- [x] Absent when the agent failed — no error shown
- [x] DECISIONS.md updated (#132–#139)

## Known limits

- **The advice can be wrong, and nothing checks it.** In testing, both models said the Anjuna flea
  market is on Saturdays (it is on Wednesdays), and one reply listed the same monastery under two
  spellings. That is why it is labelled as general advice to check locally, and why nothing in a plan
  is chosen or priced from it.
- **The plan does not follow `best_times`.** On the live Jaipur plan, Jantar Mantar is on Day 3's
  evening and its tip says mid-day. The agent runs beside the search that finds the plan's places, and
  the builder is not shown the tips (#134).
- **The places are not in the model's order.** Postgres JSONB does not keep the order of an object's
  keys: the page lists the plan's own places first, then the rest as stored.
- **The same allowance as the builder.** About 900 of the 8,000 tokens a minute Groq's free tier
  gives the model the builder writes with. A plan and a change to it inside the same minute could
  already run into that limit before this phase; the tips make it a little likelier.
- Tips cannot be asked for by hand: a plan without them gets them with its next change, or a re-plan.
- A change of interests ("more food, less history") keeps the tips the first plan got.
- English only (multi-language is Phase 36), and nothing about the agent is on the progress panel.
- CI still runs only the Python lint and unit suites (the CI phase is Phase 28).
