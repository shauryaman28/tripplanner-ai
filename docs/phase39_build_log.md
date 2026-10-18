# Phase 39 — Booking Deep Links

**Status: built, and checked on the production-like stack: a real plan's flight and hotel links, opened in Chromium, landed on Google Flights with the route, both days and both travellers filled in, and on Booking.com with the nights, the guests and the plan's own hotel first. NOT on a deployment: there is none yet.** What that leaves open is in [Known limits](#known-limits).
**What the phase delivers (roadmap):** Each flight and hotel has a "Book Now" link pointing to the correct pre-filled booking page.

Decisions: `DECISIONS.md` #354–#358. How to run it: `HOW_TO_RUN.md`, Step 37.

Three things are worth knowing before reading on.

- **The roadmap's Google Flights link makes up the day back.** `google.com/flights?q=DEL+GOI+<date>` fills in the route and the day out, and returns four days later whatever the trip; the old `#flt=` form opens on no search. Asked in its own words — "Flights to GOI from DEL on … through … for 2 adults" — Google Flights fills in all of it. Every form was opened in a browser before one was chosen (#354).
- **Booking.com turns a robot away by dropping its search.** To headless Chrome's own user agent it answered with a bare results page — today's dates, no place; to an ordinary browser's, the search as asked. The checks open it as an ordinary browser; a traveller is one.
- **The plan's hotel comes first.** Searched by name and town, Booking.com put the plan's hotel first every time we tried — even a LiteAPI sandbox flat (#355).

## What was built

```
src/ai/booking.py (NEW)                   ← a Google Flights search for a route, its days and travellers; a Booking.com
                                            search for a hotel, its nights and guests; the two sites a link may be on
src/ai/mcp_server/tools.py, models.py     ← every flight and hotel the searches return carries its `booking_url`
src/ai/builder/builder.py                 ← the plan's flight and hotel keep the search's links; a model's is dropped
src/backend/app/pdf/{plan,document}.py    ← "Book this flight on Google Flights", "Book this hotel on Booking.com": links
src/frontend: lib/booking.ts, components/BookButton.tsx (NEW); DayCard.tsx, ItineraryView.tsx, lib/types.ts
                                          ← "Book flight", "Book hotel": a new tab; greyed out with no link
scripts/booking_check.py, booking_open.js (NEW)
                                          ← a real plan's links checked, and opened in Chromium
tests/unit/test_phase39_booking.py, tests/integration/test_phase39_booking_integration.py, e2e/booking.spec.ts (NEW)
tests/e2e/stub_backend.py                 ← the stub searches' links made as the real tools make them; none for Tirthan
docs/phase39_live/                        ← the forms that were tried, and the live check
```

## Dev A — deep link generation

- **Flights** — a Google Flights search with the route from the search's own airports, the day out and the day back (or one way), the travellers, in rupees (#354).
- **Hotels** — a Booking.com search for the hotel by name and town, the check-in and check-out, the adults and the rooms the hotel search asked for (#355).
- **Stored in `structured_data`** — on the flight and the hotel of the plan, as on every result of the searches (#356).

## Dev B — booking links in the frontend

- **"Book flight" and "Book hotel"** on the flight's row and on the stay's first night; a new tab, `target="_blank"`, `rel="noopener noreferrer"` (#357, #358).
- **Greyed out with no link** — and with a link that is not one of the two searches: a `<span>` with `aria-disabled`, no `href` (#357).

## Found and fixed along the way

- **The roadmap's link form** — tried, found to invent the day back, replaced (#354).
- **A null link on every hotel of every saved plan** — the Phase 36 English record caught `"booking_url": null` on hotels with no link; a hotel without one now has no key (#356).

## The order things were done in

22:53 Phase 38 merged; the branch. The link forms were opened in Chromium before any of the phase's code (`docs/phase39_live/link_formats.txt`). 23:00 the links, the buttons and the PDF, committed. 23:07 the tests. 23:08 and 23:09 a real plan's links checked and opened on the production-like stack (`docs/phase39_live/booking_check.txt`). 23:10 the README. Then the break-on-purpose run.

## Live results

### A real plan's links, opened (18 October, 23:09)

Goa, 27 to 29 November, two travellers, on the production-like stack — real models, Duffel's and LiteAPI's sandboxes:

| | The link asks | The page showed |
|---|---|---|
| Flight (DEL → GOI) | Flights to GOI from DEL on 2026-11-27 through 2026-11-29 for 2 adults | "New Delhi to Goa \| Google Flights" — Round trip, 2, Economy; New Delhi → Goa; Departure Fri, Nov 27; Return Sun, Nov 29 |
| Hotel | Alaya Stays 1 BHK with Balcony & Pool - Veera Marina, Goa · 27 to 29 Nov · 2 adults · 1 room | Fri 27 Nov — Sun 29 Nov · 2 adults · 0 children · 1 room; first result: Alaya Stays 1 BHK with Balcony & Pool - Veera Marina |

9 of 9 checks (`docs/phase39_live/booking_check.txt`).

## Tests

| | New | Total |
|---|---|---|
| Unit + contract | 25 (`test_phase39_booking.py`) | 1,722 |
| Integration | 2 (`test_phase39_booking_integration.py`) | 108 |
| Browser | 3 (`e2e/booking.spec.ts`) | 75 |

## Break it on purpose

On a scratch copy of the repository (its own database `tripplanner_m39`, Redis number 13, and its own stub backend
and Next server on ports 8139 and 3139 for the site), each breakage alone, the tests run after it.

**21 of 22 caught** — the links (the day back dropped, the travellers dropped, a code not checked, the roadmap's form,
the town only, one room for everybody, out before in, not encoded, any https allowed), the tools (flights or hotels
without links), the builder (the hotel's link not copied, a null link saved on every stay), the PDF (no flight link,
a link on every night, any link followed), and on the site, in the browser (no new tab, the opener not cut off, no
button without a link, any link opened, "Book hotel" on every night).

**Survived: "the model's hotel link kept"** — and rightly: the builder cleared a link the model wrote, then copied
the search's over it, and the copy always runs (the hotel is always one the search found). The clearing was dead
code; it is gone, and the comment says what does the work.

## Done criterion checklist

- [x] Each flight result has a Google Flights deep link pre-filling the correct route and date — every result of the search (tests); live
- [x] Each hotel result has a Booking.com deep link — every result (tests); live
- [x] Links open to the correct search results in a browser — opened in Chromium, live
- [x] "Book Flight" and "Book Hotel" buttons, present and working — the browser test
- [x] A new tab opens — the browser test (`target="_blank"`, `rel="noopener noreferrer"`, the tab at the link)
- [x] Disabled state correct for null links — the browser test (Tirthan)

## Known limits

- **Searches, not bookings.** Each link opens a search, filled in: the traveller picks the flight and the room there. Neither site takes a booking by link, and prices there are theirs, not the plan's — the plan priced Duffel's and LiteAPI's sandboxes.
- **Booking.com, not MakeMyTrip.** The roadmap allows either; one was enough, and Booking.com's search is the one that put the plan's hotel first.
- **Only the outbound flight.** The plan knows one flight (both directions, when there is a return); the link searches both days on Google Flights, which shows the way back on the next page.
- **Google Flights and Booking.com may change their URLs.** The forms were checked on 18 October 2026; `scripts/booking_check.py --prodlike` opens them again.
