# Phase 37 — Advanced Itinerary Features

**Status: built, and checked on the production-like stack on real models and providers — every stop of a real plan with its start and end, the far ones with a travel note, no day past 22:00 — with the travel times estimated. Google's own times wait for a key of yours (HOW_TO_RUN Step 35 A): the tool is built and tested against a stand-in that answers as Google does, through the real tool server and the real Redis. NOT on a deployment: there is none yet.** What that leaves open is in [Known limits](#known-limits).
**What the phase delivers (roadmap):** Time-of-day scheduling, travel time between activities, realistic day planning.

Decisions: `DECISIONS.md` #340–#347. How to run it: `HOW_TO_RUN.md`, Step 35.

Four things are worth knowing before reading on.

- **The Distance Matrix API the roadmap names can no longer be switched on.** Since October 2026 a new Google Cloud project cannot enable it; its successor is the Routes API's route matrix, which the tool asks — without traffic, which is the time a day's cache can hold and Google's cheaper SKU (10,000 elements a month free) (#343).
- **The first real plan had three stops on a day that ended at the airport at 22:42.** The clock said, truly, that there was no time for any of them; nothing had told the model when the flight lands. The builder is now told when the first day can begin — one line in its prompt, the only one this phase adds (#340). The next plan's first day was "Explore the area", and its stops moved to the days after.
- **A test had been asking the real Groq.** It built a plan with no stand-in model, and on this machine the app hands the SDKs the real keys. The break-on-purpose run found it — on a copy with no `.env` the test failed. The tests now give the SDKs a key that opens nothing (#347).
- **The estimate was guessed, then measured.** Without Google the times are estimated from the straight line. Measured against OpenStreetMap's router on ten Goa journeys, the guess made short trips twice as long as a car takes; it now makes them as Google would — and across a river, where the road is three times the straight line, it is still far short (#344).

## What was built

```
src/ai/schedule.py                       ← NEW: a day's clock — each stop's start and end in its window, the way there
                                           (asked of the tool, or estimated), a travel note past 10 km, 08:00–22:00,
                                           the first day after the landing
src/ai/mcp_server/tools.py               ← NEW tool get_travel_time: Google's Routes API (a route matrix of one element),
                                           no traffic, cached 24 h; models.py, config.py (GOOGLE_MAPS_API_KEY), server.py,
                                           rate_limiter.py (3,000 elements a minute)
src/ai/builder/builder.py                ← the clock once the draft has passed its checks; the tool asked for every stop;
                                           the line on when the first day begins; a stop's six new fields
src/backend/app/pdf/{plan,document}.py   ← times in each stop's label, the travel line, a short visit, a late arrival
src/frontend: lib/schedule.ts (NEW), components/DayCard.tsx, lib/types.ts
                                         ← the same on the page; a travel row of its own before a far stop
scripts/schedule_check.py (NEW)          ← --route: Fort Aguada → Baga Beach on Google, twice; --prodlike: a plan's clock
scripts/provider_keys.py, cache_ttls.py  ← the Maps key asked one journey; the travel cache's day in the spec
tests/unit/test_phase37_schedule.py, tests/integration/test_phase37_schedule_integration.py, e2e/schedule.spec.ts (NEW)
tests/conftest.py                        ← no test reaches a real model through the SDKs' keys
docs/phase37_live/                       ← the estimate against OpenStreetMap's router
```

## Dev A — time-of-day scheduling

- **Realistic time slots** — morning 08:00–12:00, afternoon 13:00–17:00, evening 18:00–22:00; a stop starts when its window opens or when the traveller can be there, at the next five minutes, and stays as long as its kind of place takes (#341). In code, after the model's draft has passed every check; a model's own times are dropped.
- **A travel slot past 10 km** — a note on the stop: from where, how long, how far, by what (walk, auto-rickshaw, taxi), whose time (#342). On the page and in the PDF a row of its own.
- **`start_time`, `end_time`, `travel_from_previous_minutes`** on every stop in `structured_data`.
- **No day past 14 hours** — nothing before 08:00 or after 22:00, by construction; a stop there is no time for has no times and says so.

## Dev B — the travel-time tool

- **`get_travel_time(origin_lat, origin_lng, dest_lat, dest_lng, mode)`** — the sixth MCP tool, on Google's Routes API (#343): one element a journey, no traffic.
- **Cached for 24 hours** — the tools' Redis cache, keyed by the coordinates to a metre and the mode; `[CACHE HIT]` in the log on the second ask (unit and integration tests; `scripts/schedule_check.py --route` on Google).
- **Called by the ItineraryBuilder** — for every stop with a place on the map, all at once, through the MCP client; when it cannot answer, the schedule estimates (#344).

## Found and fixed along the way

- **A day that ends at the airport, planned full** — the first plan on the stack (#340).
- **A test asking the real Groq** — found by the break-on-purpose run's baseline (#347).
- **A free day on a late landing ran past 22:00** — found by the 500 random days (#345).
- **The estimate's numbers were guesses** — measured, and changed (#344).

## The order things were done in

00:51 the branch from main (Phase 36 merged); Google's notice read — the Distance Matrix API cannot be enabled on a new project. 01:03 the tool and the clock, committed. 01:13 the page and the PDF. 01:14 the estimate measured against OpenStreetMap's router (`docs/phase37_live/osrm_check.txt`), and changed. 01:18 the first plan on the production-like stack: three stops on a day whose flight landed at 22:42. 01:21 the builder told when the first day begins (DECISIONS #340 first, then the Phase 36 English record made again); the next plan's first day was free. 01:23 the first break-on-purpose run stopped at its baseline: a test had been asking the real Groq; 01:28 fixed.

## Live results

### A plan's clock on the production-like stack (18 October, 01:18 and 01:21)

Real Gemini and Groq, Duffel's and LiteAPI's sandboxes, OpenTripMap; no Google key, so every way is an estimate.

| | First plan (01:18) | After #340 (01:21) |
|---|---|---|
| Took | 24.5 s | 8.2 s |
| The flight | lands 22:42 on day 1 | the same |
| Day 1 | three stops, none with times (`no_time_left`) | "Explore the area" — "You arrive late" |
| Stops with times | 6 of 9 (the 3 others: no time left) | 6 of 6 |
| Travel notes | 9, for 9 stops more than 10 km away | 6 for 6 |
| Longest way | 1 h 30 min, 70.3 km (Bondla to Agonda) | 1 h 26 min, 67.1 km (the hotel to Chandor) |

The hotel the sandbox found is in the north and the attractions spread over the whole state: the clock shows what the plan had never said — an hour and a half in a taxi before the morning's stop.

### Google's times

**Not yet run: they need your `GOOGLE_MAPS_API_KEY`.** With it: `python scripts/provider_keys.py`, `python scripts/schedule_check.py --route` (the roadmap's known route, twice, the second from the cache), and `python scripts/prodlike.py up && python scripts/schedule_check.py --prodlike` (a plan whose ways are Google's).

### The estimate against OpenStreetMap's router (01:14)

Ten Goa journeys (`docs/phase37_live/osrm_check.txt`): the road 1.31–3.17 times the straight line (median 1.6); a car about 50 km/h of road. The first guess — 1.35 and town speeds of 22–28 km/h — made Fort Aguada to Baga 33 minutes where the router said 13; now 20 (13, and five to find the auto-rickshaw). The airport to Fort Aguada: 14 km as the crow flies, 44 by road around the Zuari; the estimate says 26 km — no straight line knows about the river.

### On the page and in the PDF

`docs/phase37_day_card.png` — a day on the site, its far stop's way there a row of its own ("Taxi · about 56 min ·
23.6 km from Goa Grand"); `docs/phase37_pdf_times.png` — the same in the PDF, a short visit marked.

## Tests

| | New | Total |
|---|---|---|
| Unit + contract | 40 (`test_phase37_schedule.py`) — and earlier ones that now know the sixth tool, Google's limit, the travel cache, the load-test stack's empty key, the English record | 1,662 |
| Integration | 3 (`test_phase37_schedule_integration.py`) — and Phase 24's cache test, with the travel cache's day | 104 |
| Browser | 2 (`e2e/schedule.spec.ts`) | 70 |

The roadmap's three structure tests: `test_every_stop_of_a_plan_has_its_times`, `test_a_stop_more_than_10_km_away_has_a_travel_note`, `test_no_day_is_longer_than_14_hours`.

## Break it on purpose

On a scratch copy of the repository (its own database `tripplanner_m37`, Redis number 14, and its own stub backend
and Next server on ports 8137 and 3137 for the site), each breakage alone, the tests run after it: unit, then the
integration suite for those that need the tool server or Redis, the browser test for the site's.

**The first attempt stopped at its baseline** — a unit test failed on the copy and passed here: it had been asking
the real Groq (#347). Fixed, and run again.

**First round: 37 of 38 caught.** The tool (asked without a key, nothing cached, cached an hour, traffic asked for,
a walk asked as a drive, no route taken for one, a refused key called a provider error, a metre's difference asked
again, seconds read as minutes); Google's limit lifted; the tool not offered by the server; the clock (no travel
note, a note on every stop, a day past 22:00, the way there not travelled, the landing ignored, Google's answer
ignored, two stops at once, a model's times believed, a stop with no time given times, the wrong hotel woken up in,
10:01 instead of 10:05, a flight on another day believed); the estimate (the straight line for the road, no time to
find a taxi, a walk of 5 km); the builder (no clock, the tool never asked, the landing not told, a null note saved on
every stop); the PDF (no times, no travel line, an estimate not said); the site, in the browser (no times, no travel
row, an estimate not said); the cache report without the travel cache (integration).

| Survived | Why | Now |
|---|---|---|
| key check: a 400 taken for a working key | the test checked only that a 400 was not accepted — and with the breakage it still was not, as "not what a working key gets"; what was lost was the hint that says what to look at | the test reads the hint: caught (second round, 1 of 1) |

In all, 38 of 38 caught.

## Done criterion checklist

- [x] Each activity slot has `start_time` and `end_time` — tests; the live plan (or `no_time_left`, on a day landed too late)
- [x] Activities > 10 km apart have a travel note — tests; live, 9 of 9 and 6 of 6
- [x] A day does not exceed 14 hours of activity — 500 random days; live
- [x] 3 itinerary structure tests pass
- [ ] Tool returns the correct travel time for a known route (Fort Aguada to Baga Beach) — **on Google: waits for your key**; against the stand-in, and the estimate against OpenStreetMap's router: done
- [x] Cached on the second call (`[CACHE HIT]` in logs) — unit and integration tests, through the real tool server and Redis; on Google with the key

## Known limits

- **Google's times are not yet live** — the key is yours to make (HOW_TO_RUN Step 35 A).
- **No traffic.** The times are a car's on an empty road at the hour Google assumes; Goa's roads in season are slower. Traffic would be Google's dearer SKU and a prediction for the moment it was asked.
- **Google has no auto-rickshaw.** It is asked as a car; an auto-rickshaw is slower.
- **The estimate does not know rivers** (#344) — it is what a plan gets without Google, and says "about".
- **The clock does not move stops.** A day whose stops are far apart gets short visits, marked; the model's choice of which stops share a day is not changed for distance — the first day's flight is the one thing it is told.
- **Only the outbound flight is known.** The plan has no return flight, so the last day is planned to 22:00.
