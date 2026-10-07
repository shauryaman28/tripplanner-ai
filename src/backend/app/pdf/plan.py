"""What the PDF shows, worked out from a trip and its saved itinerary.

Pure functions — no ReportLab, no network. The rules are the trip page's own,
so paper and screen agree:

  lib/map.ts            which stops are shown, which get a pin, and its number
  ItineraryView.tsx     the split into flights / stay / activities
  CostSummary.tsx       the budget note

The reader is as forgiving as the page is: whatever the page can show of an
itinerary — also one saved by an earlier version, without a flight or
coordinates — can be exported.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from app.pdf.formatting import (
    category_label,
    clip,
    describe_rating,
    format_clock,
    format_duration,
    format_inr,
    format_number,
    names_in_words,
    plural,
)
from src.ai.itinerary import FREE_TIME, SLOTS

SLOT_LABELS = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening"}


class NothingToExport(Exception):
    """The itinerary has no day-by-day plan to print."""


# ── The plan ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Stop:
    """One time slot of a day."""

    slot: str
    name: str
    cost: float
    category: str | None
    rating: float | None
    lat: float | None
    lng: float | None
    free_time: bool
    order: int | None  # the number on its map pin, 1-based within the day; None when it has no pin
    suits: tuple[str, ...] = ()  # Phase 25, a group trip: the travellers this stop is for


@dataclass(frozen=True)
class Stay:
    name: str
    cost_per_night: float
    stars: int | None
    rating: float | None  # guest review score, 0–10
    address: str | None
    lat: float | None
    lng: float | None


@dataclass(frozen=True)
class Airport:
    code: str
    name: str | None
    lat: float | None
    lng: float | None


@dataclass(frozen=True)
class Flight:
    airline: str | None
    flight_number: str | None
    departure: str | None
    arrival: str | None
    duration_mins: float | None
    price: float | None  # every traveller; both directions when the trip has a return date
    stops: int | None
    origin: Airport | None
    destination: Airport | None


@dataclass(frozen=True)
class Day:
    number: int
    date: str
    stops: tuple[Stop, ...]
    stay: Stay | None
    flight: Flight | None
    activities_cost: float

    @property
    def stay_cost(self) -> float:
        return self.stay.cost_per_night if self.stay else 0.0

    @property
    def cost(self) -> float:
        """Stay + activities — the figure a day card shows."""
        return self.stay_cost + self.activities_cost


@dataclass(frozen=True)
class Costs:
    flights: float
    stay: float
    activities: float
    total: float

    @property
    def unexplained(self) -> float:
        """What the total holds beyond the three parts.

        The builder's total may be up to ₹500 away from the sum of its parts
        (BUDGET_MATH_TOLERANCE_INR). The page prints the total it was given, so
        the PDF does too — and a ledger must add up to what it prints.
        """
        return self.total - (self.flights + self.stay + self.activities)


@dataclass(frozen=True)
class LocalTips:
    """The itinerary's local tips (Phase 22), as the page's "Local tips" section shows them."""

    transport: str | None
    customs: tuple[str, ...]
    traps: tuple[str, ...]
    best_times: tuple[tuple[str, str], ...]  # (place, when to go)
    safety: tuple[str, ...]


# The section headings, in the page's order (lib/tips.ts — a test keeps the two in step).
TIP_SECTIONS = (
    ("local_transport", "Getting around"),
    ("cultural_norms", "Local customs"),
    ("tourist_traps", "Tourist traps"),
    ("best_times", "Best times to visit"),
    ("safety_tips", "Staying safe"),
)
# A card cannot run on to the next page, so however much an itinerary holds, this much is printed.
MAX_TIPS_PRINTED = 8


@dataclass(frozen=True)
class TripPlan:
    destination: str
    start_date: date
    end_date: date
    travellers: int
    budget: float | None
    interests: tuple[str, ...]
    days: tuple[Day, ...]
    costs: Costs
    tips: LocalTips | None = None

    @property
    def nights(self) -> int:
        return max(0, (self.end_date - self.start_date).days)

    @property
    def per_traveller(self) -> float | None:
        """One traveller's share of the total, split equally; None for a trip of one, or one that costs nothing.

        The same sum an itinerary carries as `per_person_cost` since Phase 25
        (pricing.per_person) — worked out here, so that a plan saved before it
        prints its share too, and the cost page's "total ÷ travellers" is what
        the cover says.
        """
        if self.travellers < 2 or self.costs.total <= 0:
            return None
        return self.costs.total / self.travellers

    @property
    def flight(self) -> Flight | None:
        return next((day.flight for day in self.days if day.flight), None)


# ── Reading a saved itinerary ──────────────────────────────────────────────


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _amount(value: object) -> float:
    return _number(value) or 0.0


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _names(value: object) -> tuple[str, ...]:
    """A stored list of traveller names, read as forgivingly as the rest of an itinerary."""
    return (
        tuple(name.strip() for name in value if isinstance(name, str) and name.strip())
        if isinstance(value, list)
        else ()
    )


def _position(lat: object, lng: object) -> tuple[float, float] | tuple[None, None]:
    """A place on the map needs both coordinates, and they must be on the globe."""
    lat, lng = _number(lat), _number(lng)
    if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
        return None, None
    return lat, lng


def _read_airport(raw: object) -> Airport | None:
    if not isinstance(raw, dict) or not _text(raw.get("code")):
        return None
    lat, lng = _position(raw.get("lat"), raw.get("lng"))
    return Airport(code=_text(raw["code"]), name=_text(raw.get("name")), lat=lat, lng=lng)


def _read_flight(raw: object) -> Flight | None:
    if not isinstance(raw, dict):
        return None
    stops = _number(raw.get("stops"))
    return Flight(
        airline=_text(raw.get("airline")),
        flight_number=_text(raw.get("flight_number")),
        departure=_text(raw.get("departure")),
        arrival=_text(raw.get("arrival")),
        duration_mins=_number(raw.get("duration_mins")),
        price=_number(raw.get("price_inr")),
        stops=int(stops) if stops is not None else None,
        origin=_read_airport(raw.get("origin")),
        destination=_read_airport(raw.get("destination")),
    )


def _read_stay(raw: object) -> Stay | None:
    if not isinstance(raw, dict):
        return None
    lat, lng = _position(raw.get("lat"), raw.get("lng"))
    stars = _number(raw.get("stars"))
    return Stay(
        name=_text(raw.get("name")) or "Hotel",
        cost_per_night=_amount(raw.get("cost_per_night")),
        stars=int(stars) if stars else None,
        rating=_number(raw.get("rating")) or None,
        address=_text(raw.get("address")),
        lat=lat,
        lng=lng,
    )


def _read_day(raw: dict, index: int) -> Day:
    number = raw.get("day") if isinstance(raw.get("day"), int) and not isinstance(raw.get("day"), bool) else index + 1
    slots = {slot: raw[slot] for slot in SLOTS if isinstance(raw.get(slot), dict) and _text(raw[slot].get("activity"))}

    # Free time is shown once a day at most, and not at all beside a real stop (lib/map.ts dayStops):
    # the builder saves it that way now, but itineraries saved earlier repeat it in every spare slot.
    real = [slot for slot in slots if _text(slots[slot]["activity"]) != FREE_TIME]
    shown = real or list(slots)[:1]

    stops, order = [], 0
    for slot in shown:
        entry = slots[slot]
        free_time = _text(entry["activity"]) == FREE_TIME
        lat, lng = (None, None) if free_time else _position(entry.get("lat"), entry.get("lng"))
        if lat is not None:
            order += 1
        stops.append(
            Stop(
                slot=slot,
                name=_text(entry["activity"]),
                cost=_amount(entry.get("cost")),
                category=_text(entry.get("category")),
                rating=_number(entry.get("rating")),
                lat=lat,
                lng=lng,
                free_time=free_time,
                order=order if lat is not None else None,
                suits=() if free_time else _names(entry.get("suits")),
            )
        )

    return Day(
        number=number,
        date=str(raw.get("date") or ""),
        stops=tuple(stops),
        stay=_read_stay(raw.get("hotel")),
        flight=_read_flight(raw.get("flight")),
        activities_cost=sum(_amount(entry.get("cost")) for entry in slots.values()),
    )


def _read_tips(raw: object) -> LocalTips | None:
    """`structured_data["local_intelligence"]` → the tips, or None when there is nothing to print.

    Read as forgivingly as the rest: a list where a sentence should be, or an
    entry that is not text, is left out rather than failing the export.
    """
    if not isinstance(raw, dict):
        return None

    def lines(key: str) -> tuple[str, ...]:
        items = raw.get(key)
        found = [text for item in (items if isinstance(items, list) else []) if (text := _text(item))]
        return tuple(found[:MAX_TIPS_PRINTED])

    places = raw.get("best_times")
    best_times = [
        (name, when)
        for place, advice in (places.items() if isinstance(places, dict) else [])
        if (name := _text(place)) and (when := _text(advice))
    ]
    tips = LocalTips(
        transport=_text(raw.get("local_transport")),
        customs=lines("cultural_norms"),
        traps=lines("tourist_traps"),
        best_times=tuple(best_times[:MAX_TIPS_PRINTED]),
        safety=lines("safety_tips"),
    )
    has_any = tips.transport or tips.customs or tips.traps or tips.best_times or tips.safety
    return tips if has_any else None


def build_plan(
    *,
    destination: str,
    start_date: date,
    end_date: date,
    travellers: int,
    budget: float | None,
    interests: list[str] | None,
    structured_data: object,
    saved_total: float | None = None,
) -> TripPlan:
    """The trip row and its itinerary JSON → what the PDF prints. Raises NothingToExport."""
    raw_days = structured_data.get("days") if isinstance(structured_data, dict) else None
    if not isinstance(raw_days, list):
        raw_days = []
    days = tuple(_read_day(raw, index) for index, raw in enumerate(raw_days) if isinstance(raw, dict))
    if not days:
        raise NothingToExport("This itinerary has no day-by-day plan to export.")

    stay = sum(day.stay_cost for day in days)
    activities = sum(day.activities_cost for day in days)
    total = _number(structured_data.get("total_cost"))
    if total is None:
        total = _number(saved_total)
    # Itineraries saved before the flight was attached only carry it inside the total.
    flight_price = days[0].flight.price if days[0].flight else None
    flights = flight_price if flight_price is not None else max(0.0, (total or 0.0) - stay - activities)
    if total is None:
        total = flights + stay + activities

    return TripPlan(
        destination=(destination or "").strip() or "Your trip",
        start_date=start_date,
        end_date=end_date,
        travellers=max(int(travellers or 1), 1),
        budget=budget if budget and budget > 0 else None,
        interests=tuple(i.strip() for i in interests or [] if isinstance(i, str) and i.strip()),
        days=days,
        costs=Costs(flights=flights, stay=stay, activities=activities, total=total),
        tips=_read_tips(structured_data.get("local_intelligence")),
    )


# ── Figures the pages share ────────────────────────────────────────────────


@dataclass(frozen=True)
class BudgetNote:
    state: str  # "under" | "tight" | "over"
    text: str
    used: float  # total / budget
    percent: int


def budget_note(total: float, budget: float | None) -> BudgetNote | None:
    """How the total sits against the budget — the wording and thresholds of the page's cost card."""
    if not budget:
        return None
    used = total / budget
    state = "under" if used < 0.9 else "tight" if used <= 1 else "over"
    gap = format_inr(abs(budget - total))
    text = {"under": f"{gap} under budget", "tight": f"Only {gap} of the budget left", "over": f"{gap} over budget"}
    return BudgetNote(state=state, text=text[state], used=used, percent=math.floor(used * 100 + 0.5))


@dataclass(frozen=True)
class StayTotal:
    name: str
    nights: int
    cost_per_night: float

    @property
    def total(self) -> float:
        return self.nights * self.cost_per_night


def stay_totals(plan: TripPlan) -> list[StayTotal]:
    """Nights and nightly price per hotel, in the order they are stayed in."""
    nights: dict[tuple[str, float], int] = {}
    for day in plan.days:
        if day.stay:
            key = (day.stay.name, day.stay.cost_per_night)
            nights[key] = nights.get(key, 0) + 1
    return [StayTotal(name=name, nights=count, cost_per_night=price) for (name, price), count in nights.items()]


def nights_stayed(plan: TripPlan) -> int:
    """Hotel nights in the plan; the trip's own length when it has no hotel."""
    return sum(1 for day in plan.days if day.stay) or plan.nights


# ── The map ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActivityPin:
    day: int
    slot: str
    order: int
    name: str
    lat: float
    lng: float


@dataclass(frozen=True)
class HotelPin:
    name: str
    lat: float
    lng: float


@dataclass(frozen=True)
class AirportPin:
    code: str
    name: str | None
    role: str  # "origin" | "destination"
    lat: float
    lng: float


@dataclass(frozen=True)
class MapFeatures:
    activities: tuple[ActivityPin, ...]
    hotels: tuple[HotelPin, ...]
    airports: tuple[AirportPin, ...]
    routes: tuple[tuple[int, tuple[tuple[float, float], ...]], ...]  # (day, its stops in visiting order)
    days: tuple[int, ...]  # days with at least one pin
    unmapped: tuple[str, ...]  # real activities that cannot be pinned, e.g. "Day 2 · Afternoon: Some Place"

    @property
    def framed(self) -> tuple[tuple[float, float], ...]:
        """What the map frames: the destination, not the journey — every pin but the departure airport."""
        arrival = tuple((pin.lat, pin.lng) for pin in self.airports if pin.role == "destination")
        return tuple((pin.lat, pin.lng) for pin in (*self.activities, *self.hotels)) + arrival


def map_features(plan: TripPlan) -> MapFeatures:
    """The pins and routes of the web map (lib/map.ts buildMapData), for the static one."""
    activities, hotels, airports, routes, days, unmapped = [], {}, {}, [], [], []

    for day in plan.days:
        route = []
        for stop in day.stops:
            if stop.free_time:
                continue
            if stop.order is None:
                unmapped.append(f"Day {day.number} · {SLOT_LABELS[stop.slot]}: {stop.name}")
                continue
            route.append((stop.lat, stop.lng))
            activities.append(
                ActivityPin(
                    day=day.number, slot=stop.slot, order=stop.order, name=stop.name, lat=stop.lat, lng=stop.lng
                )
            )
        if route:
            days.append(day.number)
        if len(route) > 1:
            routes.append((day.number, tuple(route)))

        if day.stay and day.stay.lat is not None:
            hotels.setdefault(day.stay.name, HotelPin(name=day.stay.name, lat=day.stay.lat, lng=day.stay.lng))
        for role in ("origin", "destination"):
            airport = getattr(day.flight, role, None)
            if airport and airport.lat is not None:
                airports.setdefault(
                    airport.code,
                    AirportPin(code=airport.code, name=airport.name, role=role, lat=airport.lat, lng=airport.lng),
                )

    return MapFeatures(
        activities=tuple(activities),
        hotels=tuple(hotels.values()),
        airports=tuple(airports.values()),
        routes=tuple(routes),
        days=tuple(days),
        unmapped=tuple(unmapped),
    )


# ── The file name ──────────────────────────────────────────────────────────


def export_filename(destination: str, start_date: date, *, ascii_only: bool = False) -> str:
    """ "Goa", 2027-12-10 → "trip-goa-2027-12-10.pdf"; "गोवा" → "trip-गोवा-2027-12-10.pdf".

    Letters and digits of any script are kept and everything else becomes a
    hyphen, so nothing in the name can end a header's quoted string or name a
    folder. `ascii_only` keeps ASCII alone, accents dropped — for the plain
    `filename=` of a header, which an old client reads as Latin-1.
    """
    name = unicodedata.normalize("NFC", destination or "").lower()
    if ascii_only:
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    kept = "".join(c if unicodedata.category(c)[0] in "LMN" or c in "\u200c\u200d" else "-" for c in name)
    slug = clip(re.sub(r"-+", "-", kept).strip("-"), 60).strip("-")
    return "-".join(part for part in ("trip", slug, start_date.isoformat()) if part) + ".pdf"


# ── Wording of a row ───────────────────────────────────────────────────────


def flight_route(flight: Flight) -> str:
    """ "DEL → GOI" — whatever of the two airports is known."""
    return " → ".join(airport.code for airport in (flight.origin, flight.destination) if airport)


def flight_facts(flight: Flight) -> list[str]:
    """["6E-204", "06:00 – 08:15", "2h 15m", "Non-stop"] — the day card's line under the route."""
    times = " – ".join(clock for clock in (format_clock(flight.departure), format_clock(flight.arrival)) if clock)
    stops = "Non-stop" if flight.stops == 0 else plural(flight.stops, "stop") if flight.stops else None
    facts = (flight.flight_number or flight.airline, times, format_duration(flight.duration_mins), stops)
    return [fact for fact in facts if fact]


def stay_facts(stay: Stay) -> list[str]:
    """["4-star", "8.4/10 guest rating", "Calangute"]"""
    facts = (
        f"{stay.stars}-star" if stay.stars else None,
        f"{format_number(stay.rating)}/10 guest rating" if stay.rating else None,
        stay.address,
    )
    return [fact for fact in facts if fact]


def stop_facts(stop: Stop) -> list[str]:
    """["History", "Top attraction", "Heritage site"] — and a note when the map cannot place it."""
    facts = [category_label(stop.category)]
    if rating := describe_rating(stop.rating):
        label, heritage = rating
        facts += [label, "Heritage site" if heritage else None]
    if stop.order is None:
        facts.append("No map location for this place")
    if stop.suits:  # Phase 25: on a group trip, whose interests this stop answers
        facts.append(f"For {names_in_words(stop.suits)}")
    return [fact for fact in facts if fact]
