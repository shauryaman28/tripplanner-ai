"""Phase 38 — what a day's weather means for its plan.

The weather tool (src/ai/mcp_server/tools.get_weather) says, for each day of the trip, OWM's forecast or —
past its five days — the month's typical day, "(climate estimate)". This says what the planner makes of it:

kind      sunny, cloudy, rainy or snowy: every condition OWM or the climate table words, in one of four
setting   indoor or outdoor: where a place of each of the attraction search's categories is visited —
          a museum, a restaurant, a shop, a theatre, a temple or a church is under a roof; a beach, a fort,
          a park, a viewpoint is not
block     what the builder's model is told when a day of the trip is rainy: each day's weather, each
          attraction's setting, and the rule — on a rainy day, at least one indoor place, and indoor places
          first, as long as one is left; every other day as usual
repair    what code makes sure of afterwards, as it does for a group (src/ai/group.py): a rainy day with no
          indoor stop gets an unused indoor place — in an empty slot, or in the place of one of its outdoor
          stops — and a rainy day with none to give it says so (`weather_note`), so that the traveller keeps
          the outdoor plans flexible
attach    each day of the saved plan carries its weather (`weather`): the condition, its kind, the day's
          high and low, and whether it is an estimate
"""

from __future__ import annotations

import json

from src.ai.itinerary import FREE_TIME, SLOTS

CLIMATE_SUFFIX = " (climate estimate)"
_SUNNY = frozenset({"clear", "sunny"})
_RAINY = frozenset({"rain", "rainy", "drizzle", "thunderstorm", "showers"})
_SNOWY = frozenset({"snow", "snowy"})

# The attraction search's categories (src/ai/mcp_server/tools.py, _OTM_KIND_MAP) that are visited under a roof.
INDOOR = frozenset({"museum", "food", "shopping", "culture", "nightlife", "spiritual"})

NOTE = "Rain is forecast, and none of the places found for this trip is indoors: keep the outdoor plans flexible."


def condition_words(condition: str | None) -> str:
    """The condition without its "(climate estimate)": "Partly Cloudy (climate estimate)" → "Partly Cloudy"."""
    return (condition or "").removesuffix(CLIMATE_SUFFIX).strip()


def kind(condition: str | None) -> str:
    word = condition_words(condition).lower()
    if word in _RAINY:
        return "rainy"
    if word in _SUNNY:
        return "sunny"
    if word in _SNOWY:
        return "snowy"
    return "cloudy"  # clouds, mist, haze, fog, smoke, dust — and anything new


def setting(category: str | None) -> str:
    return "indoor" if category in INDOOR else "outdoor"


def by_date(forecasts: list[dict] | None) -> dict[str, dict]:
    """The tool's forecasts by their date; anything that is not one is left out."""
    return {
        str(f["date"]): f
        for f in forecasts or []
        if isinstance(f, dict) and f.get("date") and isinstance(f.get("temp_high_c"), (int, float))
    }


def rainy_dates(weather: dict[str, dict]) -> list[str]:
    return [day for day, forecast in weather.items() if kind(forecast.get("condition")) == "rainy"]


def block(dates: list[str], weather: dict[str, dict]) -> str:
    """The builder's paragraph — '' unless a day of the trip is rainy (a dry trip's prompt is as it was)."""
    days = [{"date": day, "weather": condition_words(weather[day]["condition"])} for day in dates if day in weather]
    if not any(kind(day["weather"]) == "rainy" for day in days):
        return ""
    return (
        f"Weather by day (JSON — a forecast, or a typical day of the month): {json.dumps(days)}\n"
        'Each attraction\'s "setting" says whether it is indoor or outdoor. On a rainy day, use at least one '
        "indoor attraction, and prefer indoor ones to outdoor ones (beaches, forts, parks) for it, as long as an "
        "unused indoor one is left. Plan every other day as usual: outdoor places are not to be avoided on them.\n\n"
    )


def _category(stop: dict, about: dict[str, dict]) -> str | None:
    return stop.get("category") or (about.get(stop.get("activity")) or {}).get("category")


def repair(draft: dict, attractions: list[dict], weather: dict[str, dict]) -> list[str]:
    """Give every rainy day an indoor stop, from the places not in the plan. Returns what was done, in words.

    A new stop goes into an empty slot of that day, or into the place of its free time, or of one of its
    outdoor stops — the last of them; on a group trip only of one whose travellers the new place suits too
    (so that Phase 25's balance holds). It costs 0, as a place group.rebalance moves in does, and the plan's
    total loses what the stop it replaced cost. A rainy day nothing can be found for gets the note.
    """
    about = {a.get("name"): a for a in attractions if a.get("name")}
    done: list[str] = []
    for day in draft.get("days") or []:
        forecast = weather.get(str(day.get("date")))
        if not forecast or kind(forecast.get("condition")) != "rainy":
            continue
        stops = {slot: day[slot] for slot in SLOTS if day.get(slot) and day[slot].get("activity") != FREE_TIME}
        if any(setting(_category(stop, about)) == "indoor" for stop in stops.values()):
            continue
        used = {(d.get(slot) or {}).get("activity") for d in draft.get("days") or [] for slot in SLOTS if d.get(slot)}
        unused = [a for a in attractions if a.get("name") not in used and setting(a.get("category")) == "indoor"]
        target, replaced = None, None
        free = [slot for slot in SLOTS if not day.get(slot) or day[slot].get("activity") == FREE_TIME]
        if free and unused:
            target, place = free[0], unused[0]
        else:
            place = None
            for slot in reversed(SLOTS):  # the evening's outdoor stop first: by then the rain has had all day
                if slot not in stops:
                    continue
                suits = set(stops[slot].get("suits") or [])
                place = next((a for a in unused if suits <= set(a.get("suits") or suits)), None)
                if place:
                    target, replaced = slot, stops[slot]
                    break
        if target is None or place is None:
            day["weather_note"] = NOTE
            done.append(f"day {day.get('day')}: rainy, and no indoor place to give it")
            continue
        day[target] = {"activity": place["name"], "cost": 0.0, "lat": None, "lng": None}
        if replaced is not None:
            draft["total_cost"] = (draft.get("total_cost") or 0.0) - (replaced.get("cost") or 0.0)
        done.append(
            f"day {day.get('day')} {target}: {place['name']} (indoor)"
            + (f" in place of {replaced['activity']}" if replaced else "")
        )
    return done


def attach(draft: dict, weather: dict[str, dict]) -> None:
    """Each day of the plan its weather — for the page's icon and the PDF's line. A day without one has none."""
    for day in draft.get("days") or []:
        forecast = weather.get(str(day.get("date")))
        if forecast:
            day["weather"] = {
                "condition": condition_words(forecast.get("condition")),
                "kind": kind(forecast.get("condition")),
                "temp_high_c": forecast.get("temp_high_c"),
                "temp_low_c": forecast.get("temp_low_c"),
                "estimate": forecast.get("source") == "climate"
                or str(forecast.get("condition", "")).endswith(CLIMATE_SUFFIX),
            }
