# Phase 38 — Weather-Aware Itinerary

**Status: built, and checked on the production-like stack on a real forecast: a week in Goa from tomorrow, rain forecast for its first day, and that day's stop indoors; the last two days a climate estimate, and said to be. NOT on a deployment: there is none yet.** What that leaves open is in [Known limits](#known-limits).
**What the phase delivers (roadmap):** Rain forecast on day 3 → outdoor activities moved to covered venues or indoor alternatives.

Decisions: `DECISIONS.md` #348–#353. How to run it: `HOW_TO_RUN.md`, Step 36.

Three things are worth knowing before reading on.

- **The weather tool had never been used by a plan, and it showed.** Only the cache warmer had asked it. It counted OpenWeatherMap's days from today, not from the trip's first; stopped where the forecast stopped; and read each day by its midnight reading, on this machine's clock. A trip starting in three days got today's weather for its first day and none after its third (#349).
- **For a place it did not know it made the weather up.** "Partly Cloudy, 32°/25°" all year, everywhere but five places — Leh in January too. On a day card that is a falsehood; such a day now shows no weather (#350).
- **On a real forecast the model did the right thing unasked to.** Rain was forecast in Goa for the trip's first day; told so, the model put a restaurant on it. Code would have moved an indoor place in if it had not, as the tests show with a stand-in model that ignores the weather (#351).

## What was built

```
src/ai/weather.py (NEW)                   ← a condition's kind; a place's setting; the builder's rainy-day paragraph; the
                                            repair; each day's weather
src/ai/mcp_server/tools.py, models.py     ← get_weather: the trip's own days (up to 15), each by its daytime at the place,
                                            the forecast then the climate (DayForecast.source), no guess for a place
                                            with no climate
src/ai/orchestrator/orchestrator.py       ← the weather read beside the hotel, attraction and local-tips searches (a row
                                            of its own), handed to the builder
src/ai/builder/builder.py                 ← on a trip with a rainy day: the weather and each attraction's setting in the
                                            prompt; the repair after the group's; every day its weather
src/backend/app/pdf/{plan,document}.py    ← the weather under each day's date, the estimate, the note
src/frontend: lib/weather.ts (NEW), components/DayCard.tsx, lib/types.ts
                                          ← the icon, the words, the "climate estimate" badge, the note
scripts/weather_check.py (NEW)            ← a real week from tomorrow, on a running backend
tests/unit/test_phase38_weather.py, tests/integration/test_phase38_weather_integration.py, e2e/weather.spec.ts (NEW)
docs/phase38_live/, docs/phase38_*.png    ← the live record; a rainy day and an estimated one on the page; the PDF
```

## Dev A — weather-aware builder logic

- **The prompt** — on a trip with a rainy day, the day-by-day weather and each attraction's setting (indoor or outdoor), and the rule: on a rainy day at least one indoor place, indoor places first, while one is left; every other day as usual (#351, #352). A dry trip's prompt is unchanged (#348).
- **No indoor alternative → a note** — "Rain is forecast, and none of the places found for this trip is indoors: keep the outdoor plans flexible."
- **The weather passed day by day** — the weather tool's days, read beside the searches and handed to the builder.
- **And code makes sure** — a rainy day the model left without an indoor stop gets one, as Phase 25's repair does for a group.

## Dev B — real weather on the page

- **An icon on each day card** — a sun, a sun and cloud, a rain cloud (and a snowflake): `[data-weather]`, from the day's `weather.kind`, itself from the forecast's condition.
- **"Climate estimate"** — a badge on a day past OpenWeatherMap's five days. In the browser test, days 6 and 7 of a week; on the stack, the same.

## Found and fixed along the way

- **The tool's three faults** — the days counted from today, nothing after the forecast, a day by its midnight (#349).
- **A guess for every place but five** (#350).
- **A rainy day was a single shower** — a day is now rainy for six hours of daytime rain, not for one reading of it.

## The order things were done in

13:30 the branch from main (Phase 37 merged). The tool's faults were found reading it for the plan, before any of the phase's code. 13:48 the weather, the rule, the repair, the page and the PDF, committed with every suite passing. 13:48:58 the real week in Goa on the production-like stack (`docs/phase38_live/weather_check.txt`). 13:50 the record kept; the README. Then the break-on-purpose run.

## Live results

### A real week, from tomorrow (18 October, 13:48)

Goa, 19 to 25 October, on the production-like stack — real models, OpenWeatherMap's real forecast, the flight and hotel sandboxes (`docs/phase38_live/weather_check.txt`):

| Day | Weather | Stops |
|---|---|---|
| 1 | Rain · 32° / 23° | Sugandha — a restaurant (indoor) |
| 2 | Sunny · 34° / 23° | an excavated site (outdoor), a car museum (indoor) |
| 3–5 | Partly cloudy | beaches, a fort, a sanctuary — and a museum, a café |
| 6–7 | Partly cloudy · 32° / 26° (climate estimate) | a beach; a free day |

The plan took 30.7 s. The warmer had asked the weather while the trip was being made, and the plan's own ask was a cache hit 18 seconds later. No repair ran: the model, told day 1 was rainy, chose the restaurant for it.

### On the page and in the PDF

`docs/phase38_rainy_day.png` — a rainy day on the site, its stop the Basilica (indoor); `docs/phase38_estimate_day.png` — a day past the forecast, its "climate estimate" badge; `docs/phase38_pdf_weather.png` — the PDF: the weather under each date, the note on a rainy day with nothing indoors, the estimate.

## Tests

| | New | Total |
|---|---|---|
| Unit + contract | 35 (`test_phase38_weather.py`) — and Phase 16's row list, with the weather's row | 1,697 |
| Integration | 2 (`test_phase38_weather_integration.py`) — and the pipeline test's parallel rows, with the weather's | 106 |
| Browser | 2 (`e2e/weather.spec.ts`) | 72 |

## Break it on purpose

On a scratch copy of the repository (its own database `tripplanner_m38`, Redis number 12, and its own stub backend
and Next server on ports 8138 and 3138 for the site), each breakage alone, the tests run after it.

**31 of 31 caught**, each by the test written for it: the tool (OWM's days counted from today, nothing after the
forecast, a week at most, rain at night or three hours of it making a rainy day, the server's clock, a guess for a
place with no climate, an estimate not marked); the weather's meaning (drizzle not rain, a temple out of doors, a dry
trip's prompt changed); the repair (a rainy day left alone, a day with an indoor place changed, a place already in
the plan moved in again, the replaced stop's cost kept, no note, a traveller's only stop given away); each day's
weather (an estimate not said); the builder (no repair, no weather on the plan, the model not shown indoor and
outdoor, a model's weather believed); the orchestrator (the weather never asked, the builder not given it, asked
again for every change); the PDF (no weather, an estimate not said); the timeline; and on the site, in the browser
(no weather on a day, no badge, one icon for every kind). None was caught by running out of time.

## Done criterion checklist

- [x] A day with a "Rainy" forecast → at least one indoor activity — the fixture through the builder; live, day 1
- [x] A day with "Sunny" → beach and outdoor activities not excluded — the same test's sunny days; live, days 2–5
- [x] Tested with a known rainy-day forecast fixture — `RAINY_DAY_TWO`
- [x] The weather icon renders for all three condition types — the browser test
- [x] The climate estimate badge is visible on day 6+ — the browser test; live, days 6 and 7

## Known limits

- **Five places have a climate.** Past the five-day forecast, anywhere but Goa, Mumbai, Delhi, Jaipur and Kerala shows no weather (#350).
- **The forecast is five days.** A trip more than four days away is planned on the month's typical day, which knows the monsoon and nothing of this year's.
- **A palace is outdoors** to the rule: the search puts palaces in one category with forts (#351).
- **The repair adds a stop, or replaces the last outdoor one;** it does not move stops between days, which would keep the plan's cost but break the group's balance and the day's clock.
