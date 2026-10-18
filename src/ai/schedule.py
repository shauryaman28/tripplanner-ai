"""Phase 37 — a day's clock: when each stop starts and ends, and how long the way to it takes.

The builder's model chooses the stops and the part of the day each is in. This decides the times, in code:
a model asked for times writes times, and nothing then says they fit.

    morning    08:00–12:00     the roadmap's windows; a stop starts when its window opens, or when the
    afternoon  13:00–17:00     traveller can be there — the stop before it over, the way there travelled —
    evening    18:00–22:00     and stays as long as such a place is usually visited (TYPICAL_MINUTES)

- **The way there** is asked of the travel-time tool (Google's Routes API) for every stop with a place on
  the map, from where the traveller is: the stop before, or the hotel they woke up in, or — on the first
  day — the airport they landed at. By what the distance suggests: a walk under 1.5 km, an auto-rickshaw
  to 10 km, a taxi past it. When the tool cannot answer (no key, a provider down), the time is estimated
  from the straight line (`estimate`), and the plan says which (`source`).
- **A travel note** (`travel`) is on every stop more than 10 km from where the traveller comes from.
- **No day is longer than 14 hours**: nothing starts before 08:00 or ends after 22:00. A stop there is no
  time left for — only ever on a first day whose flight lands late — has no times, and says so
  (`no_time_left`); one that gets less than half its usual time says that (`short_visit`).
- **The first day starts after the landing**: an hour off the plane and out of the airport, then the way
  to the first stop.

Every stop gets `start_time`, `end_time` ("HH:MM") and `travel_from_previous_minutes` (None: there is no
place to measure from or to — a free day). Free time stands alone on its day (the builder leaves it so,
`_normalise_free_time`): it has its window, or, on a day landed after it, the rest of the day.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime

from src.ai.itinerary import FREE_TIME, SLOTS

SLOT_WINDOWS = {"morning": (8 * 60, 12 * 60), "afternoon": (13 * 60, 17 * 60), "evening": (18 * 60, 22 * 60)}
DAY_START, DAY_END = 8 * 60, 22 * 60  # 14 hours: the longest a day of the plan may be
FAR_KM = 10.0  # farther apart than this, and the way between is a travel note of its own
WALK_KM = 1.5
MIN_VISIT = 30  # minutes: less than this at a place is not a visit
ROUND_TO = 5  # a plan says 10:05, not 10:01: a start is put at the next five minutes
LANDING_MINUTES = 60  # off the plane, the bags, out of the airport

# How long a place of each kind is usually visited for, in minutes — the categories of the attraction search
# (src/ai/mcp_server/tools.py, _OTM_KIND_MAP).
TYPICAL_MINUTES = {
    "beach": 150,
    "nature": 150,
    "adventure": 150,
    "nightlife": 150,
    "history": 120,
    "museum": 120,
    "culture": 120,
    "shopping": 120,
    "sports": 120,
    "food": 90,
    "spiritual": 75,
    "sightseeing": 90,
}
DEFAULT_MINUTES = 105

# The estimate when the tool cannot answer — made to look like the tool's own answer, which is Google's time
# for a car without traffic (an auto-rickshaw is asked as a car too). Measured on ten Goa journeys against
# OpenStreetMap's router (docs/phase37_build_log.md): the road was 1.31–3.17 times the straight line, 1.6 at
# the median (across a river it is three times: no straight line knows there is a river); a car's time
# without traffic, about 50 km/h of road. A walk is a walk.
ROAD_FACTOR = 1.6
SPEED_KMH = {"walk": 4.5, "auto-rickshaw": 50.0, "taxi": 50.0}
PICKUP_MINUTES = {"walk": 0, "auto-rickshaw": 5, "taxi": 5}

# Asks the travel-time tool: its parameters → {"minutes", "km", "mode", "source"}, or None when it cannot answer.
Lookup = Callable[[dict], Awaitable[dict | None]]

# What this module writes on a stop — and only it: whatever a model's reply had under these names is dropped first.
CLOCK_KEYS = ("start_time", "end_time", "travel_from_previous_minutes", "travel", "short_visit", "no_time_left")


def straight_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """The distance between two points as the crow flies (haversine)."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def mode_for(km: float) -> str:
    return "walk" if km <= WALK_KM else "auto-rickshaw" if km <= FAR_KM else "taxi"


def estimate(km: float, mode: str) -> dict:
    """A journey's time from the straight-line distance, when the tool cannot be asked."""
    road = km * ROAD_FACTOR
    minutes = max(1, math.ceil(round(road / SPEED_KMH[mode] * 60 + PICKUP_MINUTES[mode], 6)))  # 18.0000000004 is 18
    return {"minutes": minutes, "km": round(road, 1), "mode": mode, "source": "estimate"}


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _at(place: dict | None) -> tuple[float, float] | None:
    if not isinstance(place, dict):
        return None
    lat, lng = place.get("lat"), place.get("lng")
    return (float(lat), float(lng)) if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) else None


def landing(flight: dict | None, day_date: str) -> tuple[int | None, tuple[float, float] | None]:
    """When the plan's flight lands on this day (minutes after midnight), and where — or (None, None)."""
    if not isinstance(flight, dict):
        return None, None
    try:
        arrival = datetime.fromisoformat(str(flight.get("arrival")))
    except ValueError:
        return None, None
    if arrival.date().isoformat() != day_date:
        return None, None
    return arrival.hour * 60 + arrival.minute, _at(flight.get("destination"))


def _legs(plan: dict) -> list[tuple[dict, tuple[float, float], str, tuple[float, float]]]:
    """(stop, from where, that place's name, to where) — every stop with a place on the map, in order."""
    legs = []
    slept_at: dict | None = None  # the hotel of the night before
    for day in plan.get("days") or []:
        hotel = day.get("hotel")
        landed_at = landing(day.get("flight"), str(day.get("date")))[1]
        if landed_at:
            here, name = landed_at, "the airport"
        elif _at(slept_at) or _at(hotel):
            source = slept_at if _at(slept_at) else hotel
            here, name = _at(source), source.get("name") or "your hotel"
        else:
            here, name = None, None
        for slot in SLOTS:
            stop = day.get(slot)
            to = _at(stop)
            if not stop or stop.get("activity") == FREE_TIME or not to:
                continue
            if here:
                legs.append((stop, here, name, to))
            here, name = to, stop.get("activity")
        if hotel:
            slept_at = hotel
    return legs


async def add_times(plan: dict, lookup: Lookup) -> dict:
    """Give every stop of `plan` its times, in place. Returns what was asked: legs, how many Google answered."""
    for day in plan.get("days") or []:
        for slot in SLOTS:
            for key in CLOCK_KEYS:
                (day.get(slot) or {}).pop(key, None)
    legs = _legs(plan)
    asks = []
    for _stop, here, _name, to in legs:
        mode = mode_for(straight_km(here, to))
        asks.append({"origin_lat": here[0], "origin_lng": here[1], "dest_lat": to[0], "dest_lng": to[1], "mode": mode})
    answers = await asyncio.gather(*(lookup(ask) for ask in asks)) if asks else []
    travel: dict[int, tuple[dict, str, float]] = {}
    for (stop, here, name, to), ask, answer in zip(legs, asks, answers, strict=True):
        km = straight_km(here, to)
        if not answer or not isinstance(answer.get("minutes"), int):
            answer = estimate(km, ask["mode"])
        travel[id(stop)] = ({**answer, "mode": ask["mode"]}, name, km)

    for day in plan.get("days") or []:
        landed, _ = landing(day.get("flight"), str(day.get("date")))
        cursor = max(DAY_START, landed + LANDING_MINUTES) if landed is not None else DAY_START
        for slot in SLOTS:
            stop = day.get(slot)
            if not stop:
                continue
            opens, closes = SLOT_WINDOWS[slot]
            if stop.get("activity") == FREE_TIME:  # its window — or, landed after it, what is left of the day
                start = max(opens, -(-cursor // ROUND_TO) * ROUND_TO)
                stop["travel_from_previous_minutes"] = None
                if start > DAY_END - MIN_VISIT:
                    stop.update(start_time=None, end_time=None, no_time_left=True)
                else:
                    stop.update(start_time=hhmm(start), end_time=hhmm(closes if start < closes else DAY_END))
                continue
            way = travel.get(id(stop))
            minutes = way[0]["minutes"] if way else 0
            stop["travel_from_previous_minutes"] = minutes if way else None
            if way and way[2] > FAR_KM:
                answer, name, _km = way
                stop["travel"] = {
                    "from": name,
                    "minutes": answer["minutes"],
                    "km": answer.get("km"),
                    "mode": answer["mode"],
                    "source": answer.get("source", "google"),
                }
            start = max(opens, -(-(cursor + minutes) // ROUND_TO) * ROUND_TO)
            if start > DAY_END - MIN_VISIT:  # the day is over before the traveller can be here
                stop.update(start_time=None, end_time=None, no_time_left=True)
                continue
            usual = TYPICAL_MINUTES.get(stop.get("category") or "", DEFAULT_MINUTES)
            end = min(start + usual, max(closes, start + MIN_VISIT), DAY_END)
            stop.update(start_time=hhmm(start), end_time=hhmm(end))
            if end - start < usual / 2:
                stop["short_visit"] = True
            cursor = end
    return {
        "legs": len(legs),
        "from_google": sum(1 for answer in answers if answer and isinstance(answer.get("minutes"), int)),
        "far": sum(1 for way in travel.values() if way[2] > FAR_KM),
    }
