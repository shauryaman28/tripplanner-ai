# Phase 20 — Frontend Polish

**Status: ✅ Complete**
**Done criterion:** Streaming text works, the refinement flow has visual feedback, error states are handled, and the layout works on a phone.

Decisions: `DECISIONS.md` #112–#124. Two depart from the roadmap's wording: the live preview reads
the builder's JSON as it arrives rather than showing markdown (#113), and the composer is labelled
"Refine this trip:" rather than pre-filled with it (#114). The same phase fixes Phase 19's known
limit — text in the scripts of India printed as empty boxes in the PDF (#121–#124) — and one
problem a live run found in Phase 20's own error states (#116).

## What was built

```
src/ai/builder/builder.py              ← the reply is streamed: on_token sees every piece; still checked whole
src/ai/orchestrator/orchestrator.py    ← _TokenStream (builder_token events, 50 ms batches); retry_search();
                                         failed searches published in the traveller's words, with `retryable`
src/ai/utils/conversation.py           ← the run in flight, noted in Redis for GET /status
src/ai/utils/failures.py               ← NEW: a failure in the traveller's words; whether a retry can help
src/backend/app/api/routes/trips.py    ← POST /trips/{id}/retry; /status: errors, retryable, run; filename*
src/backend/app/schemas/trip.py        ← RetryRequest

src/backend/app/pdf/scripts.py         ← NEW: a font per word, HarfBuzz shaping, right-to-left lines
src/backend/app/pdf/fonts/noto/        ← NEW: 13 Noto families × Regular / Medium, with their OFL texts
src/backend/app/pdf/fonts/build_fonts.py ← builds them too; Inter / Fraunces gain Greek and Cyrillic
src/backend/app/pdf/document.py, flowables.py ← every piece of plan text goes through scripts.py
src/backend/app/pdf/formatting.py, plan.py, export.py ← clip() between syllables; the file name in any script
requirements.txt                       ← reportlab[shaping]  (adds uharfbuzz)

src/frontend/src/lib/draft.ts          ← NEW: reads the JSON that has arrived so far (pure)
src/frontend/src/components/LiveDraft.tsx ← NEW: the itinerary while it is written
src/frontend/src/components/ProgressSheet.tsx ← NEW: the planning progress as a bottom sheet on a phone
src/frontend/src/components/TripStages.tsx ← NEW: hero, start, clarify, planning, failed — out of the page
src/frontend/src/lib/useWideScreen.ts  ← NEW: which of the two layouts is in use
src/frontend/src/lib/sse.ts, changes.ts, types.ts, api.ts ← tokens; sections; failures; retryTrip; filename*
src/frontend/src/app/trips/[id]/page.tsx ← the run's section, the marks, retry, the sheet
src/frontend/src/components/AgentProgressPanel.tsx, DayCard.tsx, CostSummary.tsx, ItineraryView.tsx,
  ChatInput.tsx, ui.tsx, app/globals.css ← Retry, "Updating…" / "Updated", the composer's label

tests/e2e/stub_backend.py, tests/fakes.py ← searches that fail on purpose; a stand-in model that streams
```

## Dev A — the itinerary, token by token

`ItineraryBuilder` asks Groq with `astream`. Each piece of the reply goes to `_TokenStream`, which
publishes on the trip's SSE channel:

```
{"event": "builder_token", "agent": "itinerary_builder", "token": "…\"activity\": \"Fort Ag", "seq": 7}
```

- **Batched:** the pieces of 50 ms travel together — the model writes ~450 tokens a second. The
  first piece goes out at once: it is what tells the page writing has begun.
- **Ordered:** `seq` counts from 0 within one build. A page that joins late, or sees a gap, waits
  for the next build; a build that is retried after failing its checks starts again at 0.
- **Checked before it is saved:** the stream is the model's unchecked reply. The whole reply is
  still parsed, validated and passed by the evaluator before anything is written to the database;
  a streamed reply that fails is never saved (tested with every retry failing).

On the page, `lib/draft.ts` reads the JSON that has arrived so far — an object not yet closed, a
string cut off mid-word — and **LiveDraft** shows the days and the places named so far, with a
caret where the next word lands, under "Writing your itinerary…". The progress panel gains a
fourth step, "Itinerary — Writing", that says where the writing has got to ("Day 2 · Afternoon —
Baga Beach"). When the checked itinerary arrives, the day cards take the draft's place.

**Measured on the live model:** a three-day plan streamed as 27 events over 1.4 s (about 20 a
second, ~73 characters each), the first one 8–10 s after "Plan" (the searches come first). A
refinement streams again from `seq` 0.

## Dev B — refinement feedback, error states, the phone

**Refinement.** Under a finished plan the composer is labelled **"Refine this trip:"**. A targeted
change works on one section — flights, stay or activities — and while it runs only that section's
rows and cost tile pulse with **"Updating…"**; the rest of the plan stays still. When the new plan
arrives, what came back different is tinted green and labelled **"Updated"** for five seconds,
worked out by comparing the two plans (`changedParts()`), so nothing is marked that did not change.
A full re-plan marks the whole plan. With reduced motion, both are still states.

**A search that failed.** The progress panel says which search failed and why, in the traveller's
words, and offers **Retry** beside it — `POST /trips/{id}/retry {"agent": "hotel_agent"}` runs that
one search again and adds what it finds; the rest of the plan is kept. Where a retry cannot help
(no airport is known for the destination, nothing within budget, a date in the past) there is no
Retry: the reason says what would help instead. A page reloaded later knows all of it from
`GET /status`.

**A run that failed outright.** "That plan didn't come together", the run's reason, each failed
search with its own reason, and **Retry**, which plans the whole trip again from what the traveller
last wrote.

**A budget conflict** shows its options — cheaper flights, fewer days, a bigger budget — as tappable
cards (they were already, from Phase 10/17; now checked at 375 px).

**At 375 px** nothing scrolls sideways — checked in the browser on every view. On a phone the
planning progress is a **bottom sheet**: one line ("Searching — 1 of 3 done", "The hotel search
failed") that opens to the full panel. "Ask for a change" stacks above it and hides while it is open.

## The PDF in the scripts of India

A destination typed in Devanagari, a temple OpenTripMap names in Tamil, a monastery in Ladakh named
in Tibetan, an Urdu street name: each is drawn in the Noto font of its script and shaped by
HarfBuzz — conjuncts joined, vowel signs placed, Arabic letters joined (#121, #122). Right-to-left
words read right to left on every line, a number among them keeps its own direction, and a long
name that wraps keeps its first words on the first line (#123). Names are cut between syllables;
the download is named "trip-गोवा-2027-12-10.pdf" (#124).

Every piece of plan text passes through `scripts.py`: the cover's title and interest chips, the
running head, each day card, the stay, the ledger, the map's key. A plan written only in Latin
comes out as before. **Measured:** a Latin plan builds in 20 ms (19 before), a plan with six
scripts in 33 ms (54 ms with the map, 154 KB).

## Found and fixed along the way

| Problem | Fix |
|---|---|
| A live run with the destination typed in Devanagari: the flight search failed, and the panel showed the tool's words ("add the city to _CITY_IATA in tools.py") beside a Retry that could only fail again | Failures in the traveller's words, and Retry only where it can help (#116). The lookup itself is Phase 36's (known limits) |
| A page loaded in the middle of a refinement showed all three searches running (Phase 18's known limit) | The run in flight is noted in Redis; `/status` carries the searches it does not repeat (#117) |
| On a phone, "Ask for a change" covered the Retry in the open progress sheet — found by the 375 px test | One fixed container: the button above the sheet, hidden while the sheet is open (#118) |
| The composer's placeholder was cut off on a phone | Shorter: "a nicer hotel, cheaper flights…" |
| Phase 19: text in the scripts of India printed as empty boxes in the PDF | Noto fonts shaped by HarfBuzz (#121–#123) |
| A first version of right-to-left support kept short stretches of Urdu together with no-break spaces; a stretch long enough to wrap came out with its last words on the first line | Lines are broken in reading order and reversed one by one at drawing time (#123) |
| A shortened name could end between a letter and its vowel sign, which then prints on a dotted circle | `clip()` cuts between syllables (#124) |
| A destination with no ASCII letters lost its name in the file name (`trip-2027-12-10.pdf`) | `filename*` carries it (#124) |
| README: "Python 3.9+ also works for local dev" — the models use `X \| None`, which needs 3.10 | Corrected |

## Tests

- `tests/unit/test_phase20_streaming.py` — 51 tests. The builder hands over every piece and still
  checks the whole reply; a stream that breaks off is an LLM error; pieces travel in order, batched
  (with a patched clock); a streamed reply that fails its checks is never saved, however often it
  is retried; retry of each search, a retry that fails again (nothing rebuilt, the reason in the
  traveller's words), a retry that finds flights too dear; failures worded by code and retryable
  or not; the run-in-flight note; the `/retry` route's every answer; `/status` errors, retryable
  and the carried searches.
- `tests/unit/test_phase20_scripts.py` — 69 tests. Every script has its font in both weights and
  the build script names the same Unicode blocks; one font per word, and ReportLab never sees a
  word in two fonts; plan text escaped; joiners kept where they shape and dropped where they would
  print as boxes; HarfBuzz installed and joining (क्ष is one glyph); names cut between syllables;
  right-to-left lines checked by watching ReportLab draw them, in a paragraph, split across two
  pages, and on one line; chips as wide as their labels; every part of a real PDF in its script;
  the file named in Devanagari.
- `tests/unit/test_phase19_pdf.py` — the file name in any script, its ASCII fallback, a cut between
  syllables (97 tests).
- `tests/integration/test_pipeline_integration.py` — the tokens of a real run: in order, from 0,
  all before `planning_complete`; joined, they read as the plan that was saved — as the model wrote
  it, before the checks attached the coordinates. A refinement starts again at 0. A failed search retried and the plan keeping the rest; a failed trip retried whole; a trip
  typed in Devanagari exported with its title in Devanagari and `filename*` in the header.
- `src/frontend/e2e/draft.spec.ts` — 16 tests of the partial-JSON reader and the draft, in the browser.
- `src/frontend/e2e/polish.spec.ts` — 5 tests: the draft grows while it is written, then a change
  to the hotel marks only the stay as updating and only the stay as changed, and the marks fade;
  a failed search says why, survives a reload, and its Retry runs only that search; a search that
  would fail again offers no Retry; a run that failed outright is retried whole; every view at
  375 px, with the progress as a bottom sheet.
- `src/frontend/e2e/planning.spec.ts` — the download is named from `filename*` when there is one.

Behaviours broken on purpose, one at a time — each caught by a test: no reordering of right-to-left
lines in paragraphs, or on one line; numbers left out of a right-to-left stretch; paragraphs not
shaped; words not tagged with their font; joiners kept in Latin text; a cut inside a syllable;
`filename*` left out; the running head drawn without `scripts.py`; Retry shown for a failure that
would come back the same.

**573 unit + 7 contract, 20 integration, 34 browser (5 end-to-end flows, 8 change-summary, 16
draft, 5 polish) — all passing.** The unit and contract suites also pass in a clean
`python:3.11-slim` container (`uharfbuzz` installs from a wheel there), the rebuilt backend image
draws a Devanagari, Urdu and Tibetan PDF, and `next build` succeeds.

Verified on the real stack — backend on :8000 with the live providers and model: a Varanasi trip
streamed its itinerary as above, its refinement re-ran only the hotel search and streamed again from
`seq` 0; the same trip typed as "वाराणसी" planned, exported with "वाराणसी" on the cover and in
the running head, and downloaded as `trip-वाराणसी-2026-11-17.pdf`. That run is the one that found
the flight-search wording (#116). Groq answered one build with a 429 rate limit mid-refinement;
the build's retry absorbed it.

## Done criterion checklist

- [x] `builder_token` SSE events while the itinerary is written; the page shows it as it arrives
- [x] The final JSON is validated before the database write; a streamed reply that fails is never saved
- [x] After the itinerary, the chat input reads "Refine this trip:"
- [x] A targeted re-run animates only the section it changes; flights and activities stay still
- [x] What changed is flashed green when the re-run completes
- [x] An agent failure says which agent failed, with a Retry
- [x] A budget conflict's options as tappable cards
- [x] Every view renders without horizontal scroll at 375 px
- [x] The progress panel is a bottom sheet on a phone
- [x] Playwright test for the refinement flow
- [x] Phase 19's known limit: text in the scripts of India prints in its script, not as boxes
- [x] DECISIONS.md updated (#112–#124)

## Known limits

- **A destination typed in another script gets no flights.** The flight search looks airports up by
  English city name, so "वाराणसी" fails; the hotel and activities searches find it. The page says
  so — "write the destination in English, or as its airport code, to include flights" — and offers
  no Retry. Resolving a city in any language is Phase 36's (multi-language support).
- In the PDF, a right-to-left stretch is ordered as a browser orders it, with one simplification:
  numbers and signs *after* the last right-to-left word keep their place (a browser would pull
  "10 – 12" in a line like "سری نگر · 10 – 12 Dec" into the right-to-left run).
- Copying text out of the PDF: a shaped word in Tamil, Urdu or another script whose letters are
  reordered or joined may come out in the order of its glyphs. Printing and reading are unaffected.
- Scripts no bundled font has (Chinese, Thai, Hebrew…) still print as boxes; the running head
  leaves such a destination out.
- The stream is a preview of the model's unchecked text: for the second or so between the last
  token and the checked plan, the draft can show a place the checks then reject.
- CI still runs only the Python lint and unit suites; the frontend's type check, lint and browser
  tests are run by hand (the CI phase is Phase 28).
