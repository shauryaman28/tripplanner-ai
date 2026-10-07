"""What a trip in India costs before anything is searched, and how prices move with the seasons (Phase 21).

The searches give real prices for real dates. This module covers what they
cannot: what the same trip would cost in another month, how far a price may
move before it is booked, and — when a budget does not stretch — what a stay
and things to do typically cost there, so that the ways out of a budget
conflict carry amounts without a single extra search.

Seasons. Each destination follows a profile of twelve monthly multipliers,
relative to its cheapest month (1.0): Goa in December is 1.4 — the same trip
costs about 40% more than in July, in the monsoon. A destination not listed
follows India-wide holiday seasons. The multipliers are approximations, kept
in one table so they can be tuned (DECISIONS #125).

Typical costs are India-wide off-season figures, raised by the season's
multiplier. They are estimates: shown as "about ₹…", and never used in place
of a price a search found.

Pure: no I/O. The MCP tool `estimate_budget`, the budget decision and the
evaluator all use it, so the arithmetic is written once.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

# ── Seasons ────────────────────────────────────────────────────────────────

# Twelve multipliers, January first, against the destination's cheapest month.
SEASON_PROFILES: dict[str, tuple[float, ...]] = {
    # Beaches and backwaters: winter is high season, Christmas the peak; the monsoon empties them.
    "coast": (1.3, 1.2, 1.1, 1.05, 1.1, 1.0, 1.0, 1.0, 1.0, 1.15, 1.25, 1.4),
    # Forts, palaces and old cities of the plains: October to March; the summer heat is the off-season.
    "heritage": (1.3, 1.2, 1.1, 1.0, 1.0, 1.0, 1.05, 1.05, 1.05, 1.2, 1.3, 1.35),
    # Hill stations: the plains' summer holidays, the Puja break and the snow; the monsoon is quiet.
    "hills": (1.15, 1.0, 1.05, 1.2, 1.4, 1.35, 1.0, 1.0, 1.05, 1.2, 1.05, 1.25),
    # Ladakh and Spiti: the passes open in summer; in winter many roads close.
    "himalaya": (1.0, 1.0, 1.0, 1.0, 1.2, 1.4, 1.4, 1.35, 1.2, 1.05, 1.0, 1.0),
    # Anywhere else: India-wide holidays — summer vacations, Diwali, Christmas and New Year.
    "india": (1.1, 1.05, 1.0, 1.0, 1.1, 1.0, 1.0, 1.0, 1.0, 1.15, 1.15, 1.25),
}

# What the off-season is, in a few words, by profile and month.
_OFF_SEASON_WORDS: dict[str, dict[int, str]] = {
    "coast": dict.fromkeys(range(6, 10), "the monsoon"),
    "heritage": dict.fromkeys(range(4, 7), "the summer heat"),
    "hills": {2: "late winter", 7: "the monsoon", 8: "the monsoon"},
    "himalaya": dict.fromkeys((11, 12, 1, 2, 3, 4), "winter, when many roads are closed"),
}

# Destinations by profile. A destination is matched by a whole word of its name ("North Goa" → coast).
_PROFILE_PLACES: dict[str, tuple[str, ...]] = {
    "coast": (
        "goa",
        "gokarna",
        "kovalam",
        "varkala",
        "alleppey",
        "alappuzha",
        "kochi",
        "cochin",
        "pondicherry",
        "puducherry",
        "port blair",
        "andaman",
        "havelock",
        "neil island",
        "diu",
        "daman",
        "alibaug",
        "puri",
        "mahabalipuram",
        "mamallapuram",
        "kanyakumari",
        "digha",
        "tarkarli",
        "murudeshwar",
        "lakshadweep",
    ),
    "heritage": (
        "jaipur",
        "udaipur",
        "jodhpur",
        "jaisalmer",
        "bikaner",
        "pushkar",
        "ajmer",
        "chittorgarh",
        "agra",
        "delhi",
        "new delhi",
        "varanasi",
        "ayodhya",
        "amritsar",
        "hampi",
        "badami",
        "khajuraho",
        "orchha",
        "gwalior",
        "lucknow",
        "aurangabad",
        "ajanta",
        "ellora",
        "ahmedabad",
        "bhuj",
        "kutch",
        "mysore",
        "mysuru",
        "madurai",
        "thanjavur",
        "bodh gaya",
        "sanchi",
        "mandu",
        "ranthambore",
        "konark",
        "bhubaneswar",
    ),
    "hills": (
        "shimla",
        "manali",
        "mussoorie",
        "nainital",
        "darjeeling",
        "ooty",
        "kodaikanal",
        "munnar",
        "dharamshala",
        "mcleodganj",
        "gangtok",
        "shillong",
        "coorg",
        "kodagu",
        "dalhousie",
        "kasauli",
        "auli",
        "mount abu",
        "mahabaleshwar",
        "srinagar",
        "gulmarg",
        "pahalgam",
        "kasol",
        "chikmagalur",
        "wayanad",
    ),
    "himalaya": (
        "leh",
        "ladakh",
        "nubra",
        "pangong",
        "spiti",
        "kaza",
        "zanskar",
        "kedarnath",
        "badrinath",
        "gangotri",
        "yamunotri",  # the Char Dham: open from May, shut in winter
    ),
}

PEAK_FROM = 1.25  # a month at least this far above the cheapest is peak season
# How far a price found today may move before it is booked: the busier the season, the further.
VOLATILITY = {"peak": 0.20, "shoulder": 0.15, "off-peak": 0.10}
UNKNOWN_SEASON_VOLATILITY = 0.20  # no month given: assume the widest

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


@dataclass(frozen=True)
class Season:
    profile: str
    month: int
    multiplier: float  # prices this month against the destination's cheapest month
    label: str  # "peak" | "shoulder" | "off-peak"
    volatility: float  # how far a price may move before it is booked, as a share of it
    off_peak_months: tuple[int, ...]

    def describe(self, destination: str) -> str:
        """ "December is peak season in Goa: prices run about 40% above the off-season."."""
        month = MONTH_NAMES[self.month - 1]
        place = f" in {destination}" if destination else ""
        if self.label == "off-peak":
            return f"{month} is the off-season{place}: prices are at their lowest."
        above = round((self.multiplier - 1) * 100)
        return f"{month} is {self.label} season{place}: prices run about {above}% above the off-season."


def profile_for(destination: str | None) -> str:
    """The season profile a destination follows; "india" when it is not listed."""
    name = " ".join(re.findall(r"[a-z]+", (destination or "").lower()))
    for profile, places in _PROFILE_PLACES.items():
        if any(re.search(rf"\b{re.escape(place)}\b", name) for place in places):
            return profile
    return "india"


def season_for(destination: str | None, month: int) -> Season:
    profile = profile_for(destination)
    multipliers = SEASON_PROFILES[profile]
    multiplier = multipliers[month - 1]
    lowest = min(multipliers)
    label = "off-peak" if multiplier <= lowest else "peak" if multiplier >= PEAK_FROM else "shoulder"
    return Season(
        profile=profile,
        month=month,
        multiplier=multiplier,
        label=label,
        volatility=VOLATILITY[label],
        off_peak_months=tuple(m for m in range(1, 13) if multipliers[m - 1] <= lowest),
    )


def off_season_words(season: Season) -> str:
    """What the off-season is like there: "the monsoon", "the summer heat"… ("the off-season" if nothing more is known)."""
    return _OFF_SEASON_WORDS.get(season.profile, {}).get(season.month, "the off-season")


MIN_LEAD_DAYS = 30  # a trip moved to another month starts at least this far ahead: there is time to book it


def nearest_off_peak(destination: str | None, start: date, today: date) -> date | None:
    """The same trip in the destination's cheapest season, as soon as there is time to book it.

    The start date moves to the same day of the nearest off-peak month that is
    at least MIN_LEAD_DAYS away (clamped to the month's length). None when the
    trip is in the off-season already.
    """
    if season_for(destination, start.month).label == "off-peak":
        return None
    off_peak = set(season_for(destination, start.month).off_peak_months)
    year, month = today.year, today.month
    for _ in range(25):
        if month in off_peak:
            candidate = date(year, month, min(start.day, calendar.monthrange(year, month)[1]))
            if candidate >= today + timedelta(days=MIN_LEAD_DAYS):
                return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


# ── Typical costs (off-season, India-wide) ─────────────────────────────────

# A night in a room for two, by kind of stay. "budget" is the ₹1,000-a-night floor of DECISIONS #19,
# made realistic; the rest step up from it.
STAY_PER_ROOM_NIGHT = {"budget": 1_200, "standard": 2_500, "mid-range": 4_500, "luxury": 9_000}
STAY_WORDS = {
    "budget": "a budget hotel",
    "standard": "a 3-star hotel",
    "mid-range": "a 4-star hotel",
    "luxury": "a 5-star hotel",
}
PEOPLE_PER_ROOM = 2
# Things to do — entry fees and getting to them — per person per day (DECISIONS #19's ₹500 a day).
ACTIVITIES_PER_PERSON_DAY = 500


def rooms_for(travellers: int) -> int:
    return max(1, math.ceil(max(1, travellers) / PEOPLE_PER_ROOM))


def typical_nightly(season: Season, tier: str, travellers: int) -> float:
    """A night's stay for the whole party, at that kind of hotel, in that season."""
    return STAY_PER_ROOM_NIGHT[tier] * rooms_for(travellers) * season.multiplier


def typical_daily(season: Season, travellers: int) -> float:
    """A day's things to do for the whole party, in that season."""
    return ACTIVITIES_PER_PERSON_DAY * max(1, travellers) * season.multiplier


# ── The estimate ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Estimate:
    flights: float
    stay: float  # every night
    activities: float  # every day
    total: float
    total_min: float  # where the real cost will likely land once booked
    total_max: float
    season: Season | None  # None when no month was given
    off_peak_total: float | None  # the same trip in the destination's cheapest month; None if it is that already


def estimate(
    *,
    flights: float,
    nightly: float,
    nights: int,
    daily: float,
    days: int,
    destination: str | None = None,
    month: int | None = None,
) -> Estimate:
    """flights + nightly × nights + daily × days, with the range it may land in once booked.

    The prices are taken as they are for the trip's month — a fare found for
    those dates already carries its season. The season sets how wide the range
    is, and what the same trip would cost in the off-season.
    """
    stay = nightly * nights
    activities = daily * days
    total = flights + stay + activities
    season = season_for(destination, month) if month else None
    volatility = season.volatility if season else UNKNOWN_SEASON_VOLATILITY
    off_peak_total = total / season.multiplier if season and season.label != "off-peak" else None
    return Estimate(
        flights=flights,
        stay=stay,
        activities=activities,
        total=total,
        total_min=total * (1 - volatility),
        total_max=total * (1 + volatility),
        season=season,
        off_peak_total=off_peak_total,
    )


def typical_trip(
    *, flight_cost: float, days: int, travellers: int, destination: str | None, month: int, tier: str
) -> Estimate:
    """A trip priced with its real flights and a typical stay and things to do (nothing searched)."""
    season = season_for(destination, month)
    return estimate(
        flights=flight_cost,
        nightly=typical_nightly(season, tier, travellers),
        nights=max(0, days - 1),
        daily=typical_daily(season, travellers),
        days=days,
        destination=destination,
        month=month,
    )


# ── Each traveller's share (Phase 25) ──────────────────────────────────────


def per_person(
    *,
    total: float,
    flights: float,
    stay: float,
    activities: float,
    travellers: int,
    names: list[str] | None = None,
) -> dict:
    """A trip's cost split equally between its travellers (the shape of itinerary.PerPersonBreakdown).

    Equally, whoever a stop is for: the group goes to the fort together, and
    shares the rooms. `flights`, `stay`, `activities` and `total` are one
    traveller's part, to the paisa. `shares` are whole rupees that add up to the
    rounded total exactly — when it does not divide, the first travellers named
    carry the odd rupee. Travellers without a name are "Traveller 3", "Traveller 4".
    """
    count = max(1, travellers)
    named = [name for name in names or [] if name][:count]
    labels = named + [f"Traveller {number}" for number in range(len(named) + 1, count + 1)]
    each, odd = divmod(int(round(total)), count)
    return {
        "travellers": count,
        "flights": round(flights / count, 2),
        "stay": round(stay / count, 2),
        "activities": round(activities / count, 2),
        "total": round(total / count, 2),
        "shares": [{"name": label, "amount": each + (1 if place < odd else 0)} for place, label in enumerate(labels)],
    }


def round_inr(amount: float, step: int = 100) -> int:
    """An estimate is said in round numbers: ₹38,520 → ₹38,500."""
    return int(round(amount / step) * step)
