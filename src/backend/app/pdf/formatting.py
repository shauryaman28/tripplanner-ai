"""Number, date and place wording for the PDF.

Each function mirrors one on the trip page (lib/format.ts, lib/places.ts), so a
figure or a label reads the same on paper as it does on screen.
"""

from __future__ import annotations

import math
import re
from datetime import date

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # date.weekday(): Monday is 0


def format_inr(amount: float) -> str:
    """17200 → "₹17,200"; 150000 → "₹1,50,000" — whole rupees, Indian digit grouping."""
    rupees = math.floor(amount + 0.5)  # a half rounds up, as Math.round does on the page
    digits = str(abs(rupees))
    head, last_three = digits[:-3], digits[-3:]
    pairs = [head[max(end - 2, 0) : end] for end in range(len(head), 0, -2)][::-1]
    return f"{'-' if rupees < 0 else ''}₹{','.join([*pairs, last_three])}"


def format_number(value: float) -> str:
    """8.4 → "8.4"; 4.0 → "4" — the way JavaScript prints a number."""
    return f"{value:g}"


def parse_iso_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def format_date(day: date) -> str:
    """ "3 Oct 2026" """
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def format_date_range(start: date, end: date) -> str:
    """ "16 – 20 Nov 2026", "28 Nov – 2 Dec 2026", "30 Dec 2026 – 3 Jan 2027" """
    if start.year != end.year:
        return f"{format_date(start)} – {format_date(end)}"
    if start.month != end.month:
        return f"{start.day} {MONTHS[start.month - 1]} – {format_date(end)}"
    return f"{start.day} – {format_date(end)}"


def format_weekday(iso: str) -> str:
    """ "2026-11-16" → "Mon, 16 Nov". Anything that is not a date comes back as written."""
    day = parse_iso_date(iso)
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}" if day else str(iso)


def format_clock(iso_datetime: str | None) -> str | None:
    """ "2026-11-16T11:36:00" → "11:36". Flight times are local to the airport, so the text is read as written."""
    match = re.search(r"T(\d{2}:\d{2})", iso_datetime or "")
    return match.group(1) if match else None


def format_duration(minutes: float | None) -> str | None:
    """155 → "2h 35m" """
    if not minutes or minutes <= 0:
        return None
    hours, rest = divmod(int(minutes), 60)
    if not hours:
        return f"{rest}m"
    return f"{hours}h {rest}m" if rest else f"{hours}h"


def plural(count: int, one: str, many: str | None = None) -> str:
    """plural(1, "night") → "1 night"; plural(3, "night") → "3 nights" """
    return f"{count} {one if count == 1 else many or one + 's'}"


# ── Places (lib/places.ts) ─────────────────────────────────────────────────

# Keys are the categories the attractions tool returns (src/ai/mcp_server/tools.py).
_CATEGORY_LABELS = {
    "beach": "Beach",
    "nature": "Nature",
    "history": "History",
    "spiritual": "Spiritual site",
    "museum": "Museum",
    "culture": "Culture",
    "food": "Food",
    "nightlife": "Nightlife",
    "shopping": "Shopping",
    "wellness": "Wellness",
    "adventure": "Adventure",
    "sports": "Sports",
    "sightseeing": "Sightseeing",
}
_RATING_LABELS = ("Worth a stop", "Popular", "Top attraction")


def category_label(category: str | None) -> str | None:
    if not category:
        return None
    return _CATEGORY_LABELS.get(category) or category[0].upper() + category[1:]


def describe_rating(rating: float | None) -> tuple[str, bool] | None:
    """The attraction's popularity in words, and whether it is a heritage site.

    The rating is OpenTripMap's rate, not a star score: 1–3, or 5–7 for the
    same scale on a cultural-heritage site. 0 or missing is unrated → None.
    """
    if not rating or rating < 1:
        return None
    heritage = rating > 3
    level = min(3, max(1, math.floor((rating - 4 if heritage else rating) + 0.5)))
    return _RATING_LABELS[level - 1], heritage
