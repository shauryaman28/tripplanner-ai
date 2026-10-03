# Phase 19 — PDF Export

**Status: ✅ Complete**
**Done criterion:** The PDF downloads from the frontend, contains all itinerary content, and includes a static map image.

Decisions: `DECISIONS.md` #100–#111. Two of them depart from the roadmap's wording and are worth
knowing before reading on: the PDF is built with ReportLab rather than WeasyPrint (#100), and the
map is drawn from OpenStreetMap tiles rather than fetched from the Google Maps Static API (#101).

## What was built

```
src/backend/app/pdf/                ← NEW package
  export.py                         ← draw the map if it can be drawn, then build the PDF
  plan.py                           ← what the pages say: stops, pins, cost split, budget note, file name (pure)
  static_map.py                     ← tiles (httpx, cached in Redis) → stitched → routes and pins → JPEG (Pillow)
  document.py                       ← the pages: cover, day by day, cost breakdown, map (ReportLab)
  flowables.py                      ← pin, logo, card, chips, budget pill, meter, legend, map picture
  formatting.py, theme.py           ← ₹ / dates / place wording; the site's colours, day palette, pin geometry
  fonts/                            ← Inter ×3 + Fraunces subsets (232 KB), OFL texts, build_fonts.py
src/backend/app/api/routes/trips.py ← GET /trips/{id}/export/pdf
src/backend/app/api/deps.py         ← get_redis_or_none: Redis as an optional cache
src/backend/app/core/config.py      ← MAP_TILE_URL, MAP_ATTRIBUTION
src/backend/app/main.py             ← CORS exposes Content-Disposition and X-Itinerary-Map; version 0.19.0
requirements.txt                    ← reportlab, pillow   (requirements-dev.txt: pypdf, to read PDFs back in tests)

src/frontend/src/components/DownloadPdfButton.tsx ← NEW: the button, its loading state, the toasts
src/frontend/src/components/Toast.tsx             ← NEW: one small toast (useToast + Toast)
src/frontend/src/lib/download.ts                  ← NEW: saveBlob()
src/frontend/src/lib/api.ts                       ← downloadTripPdf()
src/frontend/src/components/ItineraryView.tsx     ← the button, beside "Map" in the "Day by day" heading
src/frontend/.eslintrc.json                       ← NEW: `npm run lint` works (next/core-web-vitals)
```

## Dev A — the endpoint

`GET /trips/{id}/export/pdf` → `200 application/pdf`, sent whole with `Content-Length`:

```
Content-Disposition: attachment; filename="trip-goa-2027-12-10.pdf"     ← destination, first day of the trip
X-Itinerary-Map: included | unavailable | none
Cache-Control: private, no-store
```

`401` without a token; `404` for a trip that is not yours, a trip with no itinerary yet, or an
itinerary with no day-by-day plan; `500` in the usual error envelope if the build itself fails.
It is always the latest itinerary version, so a refinement is in the next download.

**The four sections** (A4, the site's two typefaces embedded, a PDF outline, "Page 2 of 5"):

| Section | What is on it |
|---|---|
| Cover | Destination, dates, nights, travellers, interests; the estimated total against the budget (the page's cost card: figure, "₹32,800 under budget", the meter); flight, stay and plan at a glance; "nothing has been booked" |
| Day by day | One card per day in the day's colour: flight, morning / afternoon / evening with category and popularity, the stay, the day's cost. Each stop wears the pin it has on the map. A card is never split across two pages |
| Cost breakdown | Flights + stay + activities → total, budget, what is left or over, per traveller; then each day's share. The lines add up to the total printed |
| Map | The picture, a legend, and a key that spells out every numbered pin; places without a location are listed under it |

**The map** is drawn, not fetched: the tiles of one view (20 at most) are stitched, toned down like
the web map, and the routes and pins are drawn on top — a day's colour and shape, the stop's
number, the hotel in gold, the arrival airport. Pins that would cover each other are nudged apart.
Tiles are cached in Redis for a week.

**When the map cannot be drawn** (tile server down, a body that is not an image, slower than 12 s):
the map page is left out, a warning is logged with the trip id, and the PDF still downloads —
`X-Itinerary-Map: unavailable`. With nothing to place on a map, or `MAP_TILE_URL` empty: `none`, and no warning.

**Measured:** a build takes about 110 ms with the map and 20 ms without (two layout passes each).
Exporting the real Kedarnath itinerary (9 days, 5 pages, 335 KB): 0.35 s with no tiles cached
(16 tiles), 0.14 s once they are. Without the map the same PDF is 60 KB.

## Dev B — the button

- **Download PDF** sits beside "Map" in the "Day by day" heading; on a phone the two drop under it.
- Click → `fetch` with the token → blob → a temporary link → the browser saves the file under the
  name the backend gave it.
- **Loading:** the button is disabled and reads "Preparing PDF…" with a spinner. It is also disabled
  while a change to the plan is running — the file would be out of date before it was opened.
- **Failure:** any non-200 answer → the toast "PDF generation failed — try again" (`role="alert"`),
  and the button works again. A `401` goes to the sign-in page instead.
- **Without the map:** the file is saved, and a note says it came without the map this time.

## Found and fixed along the way

| Problem | Fix |
|---|---|
| WeasyPrint could not be imported on the dev laptop (`cannot load library 'libgobject-2.0-0'`) | ReportLab — pure Python, same PDF in tests, CI and Docker (#100) |
| On the first real itinerary, the hotel and two stops sat on one spot: one pin hid the other two | Pins that would hide each other are nudged apart (#104) |
| Building PDFs from several threads at once crashed inside ReportLab (40 of 96 builds) | One build at a time; the test for it fails on every run if the lock is removed (#106) |
| Drawing the map on one oversampled layer took 143 MB for every map | Each pin and route is antialiased on its own small picture: 55 MB (#106) |
| The test stacks share Redis with the dev servers and cached blank test tiles under the real tile server's keys | The stubs answer for a tile server of their own; asserted in the integration test (#109) |
| A tile-server error message carries the tile URL, and with it a hosted provider's key | The message is rebuilt without it; tested with a key in the URL (#102) |
| A rate-limit page answered with `200` would have been cached as "the tile" for a week | Tiles are checked to be images before they are kept |
| One endless name in an itinerary made the whole PDF impossible to lay out (a card cannot run on to the next page) | Names and detail lines are cut to a length that always fits |
| `PYTHON=../../.venv/bin/python npx playwright test` (README) never worked | Playwright finds the repo's `.venv` itself (#110) |
| The web map's default tile URL used the `{s}` subdomains OpenStreetMap's policy no longer allows | One URL, as the policy names it (#101) |
| `npm run lint` asked to be set up (Phase 18's known limit) | ESLint configured; the code passes as it stands (#111) |
| `Itinerary.content` was documented as "used by PDF export" — nothing writes it | The docstring says what the export really reads |

## Tests

- `tests/unit/test_phase19_pdf.py` — 94 tests. Formatting matches the trip page (₹ in lakhs, date
  ranges, popularity wording); the day palette and category labels are read out of the TypeScript
  source and compared; the plan (stops, pin numbers, cost split, budget thresholds, legacy
  itineraries, malformed JSON); the map (projection worked by hand, framing, the tile grid, pins
  drawn where the place is, the cache, every failure mode, no URL in any error); the document
  (built for real and read back with `pypdf`: four sections, the image on the map page only,
  page numbers, fonts embedded, text never treated as markup, a 15-day trip, endless names,
  concurrent builds);
  the export (the map is the only part allowed to be missing); the route (headers, 401, 404s,
  500 envelope, CORS).
- `tests/integration/test_pipeline_integration.py` — the export in the real pipeline: 404 before
  planning, the PDF after it, tiles in Redis for a week under the stub's keys, and the new hotel
  in the PDF after a refinement. A second test: only the owner can export; a dead tile server
  costs the map, not the PDF.
- `src/frontend/e2e/planning.spec.ts` — a real download in the browser (file name, `%PDF-`, size),
  the loading state, the note when the map is missing, the error toast, and a retry that works.

Twelve behaviours were broken on purpose, one at a time (plan text as markup, a map failure
failing the export, the tile URL in the error, the budget threshold, export of an old version,
anyone exporting any trip, …): each was caught by a test. One was not at first — caching a tile
before checking it — and the test for it was tightened.

**457 unit + contract, 17 integration, 13 browser (5 end-to-end flows + 8 change-summary) — all passing.**
The unit and contract suites also pass in a clean `python:3.11-slim` container (fresh install, no
Pango / GObject / Cairo), and the rebuilt backend image exports a real trip.

Verified on the real stack — backend on :8000, the dev frontend on :3000, real OpenStreetMap
tiles: the Kedarnath trip planned on live APIs downloads from the button as
`trip-kedarnath-2026-10-02.pdf` (5 pages, map with 9 pins and the hotel). With an unreachable tile
server the same trip still downloads, one page shorter, and the backend logs the warning. With
Redis unreachable it downloads with the map. Screens checked at 1440 px and 375 px (no sideways scroll).

## Done criterion checklist

- [x] Server-side PDF generation; the WeasyPrint / ReportLab choice made and documented (#100)
- [x] `GET /trips/{id}/export/pdf` → `Content-Type: application/pdf`, `Content-Disposition: attachment; filename=trip-{destination}-{date}.pdf`
- [x] Cover page: destination, dates, total cost
- [x] Day-by-day pages: morning / afternoon / evening, hotel, day cost
- [x] Cost breakdown page: flights + hotels + activities
- [x] Static map image with the activity coordinates as markers — from map tiles instead of Google's API (#101)
- [x] Map failure → the image is omitted, a warning is logged, the PDF still downloads
- [x] "Download PDF" button on the itinerary view: fetch → blob URL → download
- [x] Loading state visible while the file is generated
- [x] Non-200 → toast "PDF generation failed — try again"
- [x] DECISIONS.md updated (#100–#111)

## Known limits

- ~~Text in a script other than Latin (a destination typed in Devanagari) prints as empty boxes; the
  PDF is still built and named by the trip's date. The bundled fonts are Latin subsets.~~ Fixed in
  Phase 20: the scripts of India are drawn in Noto and shaped by HarfBuzz, and the file is named
  in the destination's own script (DECISIONS #121–#124, `docs/phase20_build_log.md`).
- The PDF is A4 only, and it is not a tagged (screen-reader) PDF — ReportLab's open-source edition
  does not write tags. It does carry a title, a language and an outline.
- OpenStreetMap's tile server is fine for development; a deployment with real traffic should set
  `MAP_TILE_URL` (and the frontend's `NEXT_PUBLIC_MAP_TILE_URL`) to a provider it has an account with.
- The export is always of the latest itinerary. Earlier versions are listed by
  `GET /trips/{id}/itineraries` but cannot be downloaded as PDFs.
- CI still runs only the Python lint and unit suites; the frontend's type check, lint and browser
  tests are run by hand (the CI phase is Phase 28).
- Carried over from Phase 18: activities have no prices, and a plan can come out over the budget
  (the cover and the cost page say so in words).
