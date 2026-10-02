# Phase 18 — Map View

**Status: ✅ Complete**
**Done criterion:** Every itinerary shows attraction pins on a map, a route drawn per day, and a detail popup on pin click.

This phase also redesigned the whole frontend (sign-in, trips list, trip page) and fixed the
problems that running it for real turned up. Decisions: `DECISIONS.md` #75–#89.

## What was built

```
src/ai/itinerary.py               ← NEW: SLOTS and FREE_TIME, shared by builder / evaluator / orchestrator / embedder
src/ai/mcp_server/models.py       ← Airport; Flight.origin / Flight.destination; Hotel.lat / lng
src/ai/mcp_server/tools.py        ← flights carry their airports; attractions without coordinates are dropped;
                                    category from the most specific OpenTripMap kind; rating = source rate or 0;
                                    attractions cache key is versioned (v2)
src/ai/builder/builder.py         ← shape validated first; free time said once per day; _attach_source_data():
                                    coordinates, category, rating, hotel details and the outbound flight
src/ai/orchestrator/orchestrator.py ← _unmapped_activities() warning in the save path; escalate row stores the
                                    conflict options; summaries read "Found 1 flight"
src/backend/app/api/routes/trips.py ← GET /status returns budget_conflict {reason, options}
src/backend/app/main.py           ← restart recovery: an interrupted refinement goes back to "completed"

src/frontend/src/lib/map.ts       ← buildMapData(): itinerary → pins, routes, unmapped; day colours and shapes
src/frontend/src/lib/places.ts    ← how a category and a rating are shown
src/frontend/src/lib/format.ts    ← money, dates, durations
src/frontend/src/components/ItineraryMap.tsx  ← Leaflet map (react-leaflet), loaded with ssr: false
src/frontend/src/components/{DayCard,CostSummary,ItineraryView,AgentProgressPanel,MessageThread,ChatInput}.tsx
src/frontend/src/components/{AppHeader,Brand,ui}.tsx
src/frontend/src/app/{login,trips,trips/[id]}/page.tsx, globals.css, layout.tsx, icon.svg
src/frontend/tailwind.config.ts   ← design tokens
```

## Dev A — coordinates

- **Always populated at the source.** OpenTripMap returns a `point` for every place; one without it is
  dropped, the same way an unnamed one is. Hotels (LiteAPI) and airports (Duffel) carry coordinates too.
- **Attached by code, not by the model.** The builder prompt (v6) asks the model to copy `lat`/`lng`,
  but the draft is then overwritten from the source data by activity name. Names have already passed
  the data-scope check, so the lookup is exact.
- **Also attached:** `category` and `rating` per activity, hotel `stars` / `rating` / `address` /
  `lat` / `lng`, and the outbound flight on day 1 — the cheapest one, which is the flight the budget
  math already assumes. A flight the model put on any other day is removed.
- **Save path:** `persist_node` logs a warning listing any real activity without coordinates and
  records it as `unmapped_activities` on its `agent_runs` row. It never fails the trip. Free time is
  not a place and is not counted.

**Measured on live runs:** Goa (5 days) 8 of 8 activity slots with coordinates; Jaipur (4 days) 8 of 8.
100% against the 95% target, `unmapped_activities: []` both times.

## Dev B — the map

- `leaflet` 1.9 + `react-leaflet` 4 (the React 18 line). OpenStreetMap tiles, desaturated in CSS so
  the pins carry the colour. The tile URL is configurable (`NEXT_PUBLIC_MAP_TILE_URL`).
- **Pins** are coloured by day (day 1 blue, day 2 green, then wine, orange, forest, sky, violet) and
  numbered in visiting order within the day. After seven days the colours repeat with a different
  shape (circle → square → diamond), so a 21-day trip still has a distinct mark per day. The hotel
  pin is a gold square with a bed icon; the flight's two airports are dark markers with a plane.
- **Routes:** one `Polyline` per day joining its stops morning → afternoon → evening, in the day's
  colour over a white casing. A day with one stop has no line.
- **Popup:** name, day and time slot, category, rating ("Top attraction (3 of 3)"), heritage site,
  cost estimate. Hotels show stars, guest score, address and nightly price; airports say which end
  of the flight they are.
- **Legend** above the map names every day, the hotel and the airport. The day entries are buttons:
  one isolates that day (the others fade and the view re-fits).
- **Day cards and map are linked:** every stop in a day card wears the same pin, and its "Map" button
  scrolls to the map and opens that pin's popup.
- **Framing:** the view fits the activities, hotel and arrival airport. The departure airport is
  drawn but not framed — including Delhi would zoom a Goa trip out to half of India.
- **Missing coordinates:** the place is skipped, listed under the map, and its day card says
  "No map location for this place". An itinerary with nothing mappable shows a note instead of a map.

## The redesign

One small design system — tokens in `tailwind.config.ts`, a handful of component classes in
`globals.css` (`.btn`, `.card`, `.input`, `.chip`, `.pin`) — and no component library.

- **Sign-in:** split layout with a preview of an itinerary; show/hide password; a clear message for a
  wrong password.
- **Trips:** cards with a cover, status badge (icon + words, never colour alone), dates, travellers,
  budget; new-trip form with a traveller stepper and interest suggestions; loading and empty states.
- **Trip page:** destination header; cost card (one large total, a budget meter, flights / stay /
  activities tiles); day cards; map; the assistant beside the plan with live progress, the
  conversation and one-tap suggestions.
- **Small screens:** the plan comes first and the assistant follows it; a floating "Ask for a change"
  button leads to the composer whenever it is off screen. Before there is a plan, the assistant is
  the page.
- **States:** not planned yet, planning, needs your answer, over budget (the three ways out as
  cards), failed, updating (the current plan stays on screen, dimmed, until the new one replaces it).

## Found and fixed along the way

| Problem | Fix |
|---|---|
| Day colours picked by eye: blue and purple were indistinguishable with red-green colour blindness | Palette chosen by search and validated for every pair (#81) |
| A wildlife sanctuary and a zoo were labelled "history" | Category comes from the most specific kind (#83) |
| "Rating 7" on a 1–3 scale, and an invented 3.0 for unrated places | Rating shown as popularity + heritage; unrated shows nothing (#83) |
| A free day read "Explore the area" three times | Free time is said once per day (#79) |
| A malformed slot from the model crashed the builder | Shape is validated before anything reads the draft (#80) |
| Reloading an over-budget trip lost the three options | `GET /status` returns them (#85) |
| A refinement interrupted by a restart marked a planned trip "failed" | Recovery puts it back to "completed" (#86) |
| Playwright shared ports and a database with the dev servers | It runs on its own stack (#87) |
| After a refinement the progress panel showed untouched searches as "not started" | The panel knows what a refinement repeats (#88) |
| "Found 1 flights" | Counted in plain English |
| Attractions cached before this phase would be served with the old categories for 6 hours | Cache key versioned (#83) |
| The logo was blank on the phone sign-in page | Each logo has its own SVG gradient id |

## Tests

- `tests/unit/test_phase18_map.py` — 33 tests: coordinates / category / rating attached from the
  source and wrong ones corrected; hotel and flight attached, a flight on another day dropped;
  free-time normalisation; 18 category cases taken from real OpenTripMap `kinds`; rating never
  invented; attractions without coordinates dropped; cache key version; flights carry airports;
  the unmapped report; the warning fires and the trip still saves.
- `tests/unit/test_itinerary_builder.py` — malformed shapes → `SCHEMA_INVALID`; a null cost is accepted.
- `tests/unit/test_pipeline_regressions.py` — `budget_conflict` in `GET /status` (there after a
  conflict, gone once a later run supersedes it); summary wording.
- `tests/integration/test_pipeline_integration.py` — the options a real run stores come back from
  `GET /status` unchanged; restart recovery.
- `src/frontend/e2e/planning.spec.ts` — 4 browser tests: plan → cards → map (pin counts per day,
  colours, routes, popup, legend filter, the day card's "Map" button) → reload → refine; a budget
  conflict whose options survive a reload and re-plan; login; redirect when signed out.

**341 unit + contract, 15 integration, 4 browser E2E — all passing.**

Verified in the browser on the real backend (Duffel and LiteAPI sandbox data): Jaipur planned in
19 s — 8 stops, 4 routes, hotel and airport pins — then two refinements; an over-budget Udaipur
trip reloaded with its options intact. Desktop (1440 px) and phone (iPhone 13) layouts checked for
every state.

## Done criterion checklist

- [x] `Attraction.lat` / `lng` always populated; no frontend geocoding
- [x] Builder output always carries `lat` / `lng` for real activities (prompt v6 + source attach)
- [x] Warning (not failure) for activities without coordinates; recorded on the `persist` run
- [x] 95%+ of activity slots have coordinates (100% on both live runs)
- [x] Map renders below the day cards with correct pins for a real itinerary
- [x] Pins colour-coded by day; hotel gold; flight origin/destination info markers
- [x] Polyline connects each day's activities in order
- [x] Pin click shows the detail popup
- [x] Missing coordinates do not crash the map
- [x] DECISIONS.md updated (#75–#89)

## Known limits

- Activity costs are still 0: attractions carry no price, so "Activities ₹0 — no entry fees listed".
- A plan can come out over the user's budget (the budget check only gates the flights). The cost
  card says so; nothing blocks it.
- A refinement can return the same plan (the search found nothing better). The assistant says that
  instead of "done".
- Reloading the page in the middle of a targeted refinement shows all three searches as running
  until it finishes: the page cannot know which one is being repeated.
- ESLint is not configured for the frontend (`npm run lint` asks to set it up). Type-checking
  (`npx tsc --noEmit`) and the production build are the static checks. *(Configured in Phase 19.)*
