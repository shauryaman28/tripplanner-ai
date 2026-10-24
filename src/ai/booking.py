"""Phase 39 — where a traveller books what the plan found: a search on Google Flights or Booking.com, filled in.

Each link was opened in a browser before it was written (docs/phase39_build_log.md), and is the form that fills
the page in:

flight   Google Flights, asked in its own words — "Flights to GOI from DEL on 2026-11-20 through 2026-11-23
         for 2 adults" (or "One way flights to …"), in rupees. The roadmap's `google.com/flights?q=DEL+GOI+<date>`
         fills in the route and the day out but makes up a day back; the `#flt=` form opens on no search at all.
hotel    Booking.com's search for the hotel by its name and its town — "Goa Grand, Goa" — with the dates, the
         adults and the rooms the hotel search asked for (two adults to a room, src/ai/mcp_server/tools.py), in
         rupees. The hotel itself comes first in what it finds; a name it does not know still searches the town.

Nothing a link holds is anybody's but the search's: codes, dates, a count of travellers, a hotel's name and a
town, each URL-encoded. A link that cannot be made — a code that is not one, a date that is not one — is None,
and the page shows its button greyed out.
"""

from __future__ import annotations

import math
import re
from datetime import date
from urllib.parse import quote, urlencode

GOOGLE_FLIGHTS = "https://www.google.com/travel/flights"
BOOKING_COM = "https://www.booking.com/searchresults.html"

_IATA = re.compile(r"^[A-Z]{3}$")
# What a link to book must begin with — the two searches this makes (src/frontend/src/lib/booking.ts says the same,
# and opens nothing else; a test keeps the two in step)
BOOKING_SITES = (f"{GOOGLE_FLIGHTS}?", f"{BOOKING_COM}?")
MAX_TRAVELLERS = 9


def is_booking_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith(BOOKING_SITES)


def _iso(value: object) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _adults(count: object) -> int | None:
    return count if isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= MAX_TRAVELLERS else None


def flight_link(
    origin: object, destination: object, depart: object, back: object = None, travellers: object = 1
) -> str | None:
    """A Google Flights search for this route, these days and this many travellers — or None."""
    origin, destination = str(origin or "").strip().upper(), str(destination or "").strip().upper()
    out, home, adults = _iso(depart), _iso(back) if back else None, _adults(travellers)
    if not (_IATA.match(origin) and _IATA.match(destination) and out and adults) or (back and not home):
        return None
    asked = (
        f"Flights to {destination} from {origin} on {out} through {home}"
        if home
        else f"One way flights to {destination} from {origin} on {out}"
    )
    if adults > 1:
        asked += f" for {adults} adults"
    return f"{GOOGLE_FLIGHTS}?{urlencode({'q': asked, 'curr': 'INR', 'hl': 'en'}, quote_via=quote)}"


def hotel_link(name: object, town: object, check_in: object, check_out: object, guests: object = 1) -> str | None:
    """A Booking.com search for this hotel in this town, these nights and this many guests — or None."""
    place = str(town or "").strip()
    hotel = str(name or "").strip()
    first, last, adults = _iso(check_in), _iso(check_out), _adults(guests)
    if not (place and first and last and adults) or last <= first:
        return None
    params = {
        "ss": f"{hotel}, {place}" if hotel else place,
        "checkin": first,
        "checkout": last,
        "group_adults": adults,
        "no_rooms": math.ceil(adults / 2),  # what the hotel search asked LiteAPI for: two adults to a room
        "group_children": 0,
        "selected_currency": "INR",
    }
    return f"{BOOKING_COM}?{urlencode(params, quote_via=quote)}"
