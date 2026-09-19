"""
Phase 12 Dev A — ItineraryBuilder: Groq (Llama 3.3) + Structured Synthesis.

Produces the day-by-day JSON schema defined in the roadmap:

    {
      "days": [
        {
          "day": 1, "date": "2025-12-10",
          "morning": {"activity": "...", "cost": 0, "lat": 15.5, "lng": 73.8},
          "afternoon": {"activity": "...", "cost": 0, "lat": null, "lng": null},
          "evening": {"activity": "...", "cost": 0, "lat": null, "lng": null},
          "hotel": {"name": "...", "cost_per_night": 4500},
          "flight": null
        }
      ],
      "total_cost": 48500,
      "currency": "INR"
    }

Two deterministic Python validations run on every LLM output, mirroring the
Phase 7/10/11 precedent (DECISIONS.md #6, #18, #21) of keeping objectively
verifiable checks out of the LLM's hands:

  1. Data-scope enforcement — every activity/hotel name referenced in the
     draft must come from the flights/hotels/attractions actually passed in
     (or be the documented fallback phrase). Catches hallucination before
     the Evaluator (Phase 11) ever sees the draft.
  2. Budget-math consistency — sum of per-day costs (+ cheapest flight) must
     be within ₹500 of the declared total_cost. Catches a builder that
     writes an internally-inconsistent total.

These are builder-internal sanity checks. EvaluatorAgent (Phase 11) still
runs afterward and independently re-checks hallucination + budget-vs-
estimate_budget consistency — the two checks are not redundant: this
module validates internal consistency of the JSON itself; the Evaluator
validates the draft against trip dates and the wider budget estimate.

LLM note: originally designed for Claude Haiku 4.5 but swapped to
Groq (llama-3.3-70b-versatile) for free-tier usage. The function is
named _call_llm to be model-agnostic. Swap the ChatGroq line for any
LangChain-compatible chat model without touching callers.
"""

from __future__ import annotations

import json
import logging
import uuid

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai.utils.run_logger import log_agent_run, timed_run

logger = logging.getLogger(__name__)

BUDGET_MATH_TOLERANCE_INR = 500.0
# Phrases this builder is documented to write when source data is empty for
# a slot. The Evaluator's hallucination check (evaluator.py) maintains an
# identical copy of this set so both layers stay in sync.
_ALLOWED_FALLBACK_PHRASES = {"Explore the area"}


# ── Draft schema ─────────────────────────────────────────────────────────


class ActivitySlot(BaseModel):
    activity: str
    cost: float = 0.0
    lat: float | None = None
    lng: float | None = None


class HotelSlot(BaseModel):
    name: str
    cost_per_night: float = 0.0


class DaySchedule(BaseModel):
    day: int
    date: str
    morning: ActivitySlot | None = None
    afternoon: ActivitySlot | None = None
    evening: ActivitySlot | None = None
    hotel: HotelSlot | None = None
    flight: dict | None = None


class ItineraryDraft(BaseModel):
    days: list[DaySchedule]
    total_cost: float
    currency: str = "INR"


class BuilderError(BaseModel):
    error: str
    code: str


# ── Prompt ───────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an expert travel itinerary writer. You will be given \
flights, hotels, and attractions data for a trip, and must produce a single JSON \
object — a structured day-by-day itinerary — and nothing else.

Return ONLY a JSON object with this exact shape:
{
  "days": [
    {
      "day": 1,
      "date": "YYYY-MM-DD",
      "morning": {"activity": "<name>", "cost": <number>, "lat": <number|null>, "lng": <number|null>},
      "afternoon": {"activity": "<name>", "cost": <number>, "lat": <number|null>, "lng": <number|null>},
      "evening": {"activity": "<name>", "cost": <number>, "lat": <number|null>, "lng": <number|null>},
      "hotel": {"name": "<name>", "cost_per_night": <number>},
      "flight": null
    }
  ],
  "total_cost": <number>,
  "currency": "INR"
}

CRITICAL DATA SCOPE RULE:
- Only reference activity names that appear verbatim in the provided attractions list.
- Only reference the hotel name that appears verbatim in the provided hotels list.
- NEVER invent an activity, hotel, or place name that was not given to you.
- If there is no attraction data available for a day, set that slot's activity to
  exactly "Explore the area" and cost to 0 — do not invent a substitute.

CRITICAL BUDGET RULE:
- total_cost MUST equal the sum of every activity cost + every hotel cost_per_night
  (one charge per day) + the flight cost, within ₹500. Do not round loosely.
- Compute total_cost as the final step: add up every activity cost, every
  hotel cost_per_night (once per night), and the flight cost. Do not state
  total_cost as an independent guess.

Return ONLY the JSON object. No explanation, no markdown code fences."""


def _build_user_prompt(
    trip_meta: dict,
    flights: list[dict],
    hotels: list[dict],
    attractions: list[dict],
) -> str:
    return (
        f"Trip: {trip_meta.get('destination')} "
        f"from {trip_meta.get('start_date')} to {trip_meta.get('end_date')}, "
        f"{trip_meta.get('group_size', 1)} traveller(s).\n\n"
        f"Available flights (JSON): {json.dumps(flights)}\n\n"
        f"Available hotels (JSON): {json.dumps(hotels)}\n\n"
        f"Available attractions (JSON): {json.dumps(attractions)}\n\n"
        "Build the itinerary now, respecting the data-scope and budget rules exactly."
    )


async def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call the configured LLM and return the raw text response.

    Using Groq (llama-3.3-70b-versatile) — free tier, no credit card required.
    Swap ChatGroq for any LangChain-compatible chat model (e.g. ChatAnthropic,
    ChatOpenAI) without changing any other code; this function is the single
    seam that tests patch.
    """
    from langchain_groq import ChatGroq

    llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=4096)
    response = await llm.ainvoke([("system", system_prompt), ("user", user_prompt)])
    return response.content


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json")
    return text.strip()


def _validate_data_scope(draft: dict, hotels: list[dict], attractions: list[dict]) -> list[str]:
    known_activities = {a.get("name") for a in attractions if a.get("name")}
    known_hotels = {h.get("name") for h in hotels if h.get("name")}
    violations: list[str] = []

    for day in draft.get("days", []):
        for slot_name in ("morning", "afternoon", "evening"):
            slot = day.get(slot_name)
            if not slot or not slot.get("activity"):
                continue
            name = slot["activity"]
            if name not in known_activities and name not in _ALLOWED_FALLBACK_PHRASES:
                violations.append(f"day {day.get('day')} {slot_name}: unknown activity '{name}'")

        hotel = day.get("hotel")
        if hotel and hotel.get("name") and hotel["name"] not in known_hotels:
            violations.append(f"day {day.get('day')} hotel: unknown hotel '{hotel['name']}'")

    return violations


def _cheapest_flight_cost(flights: list[dict]) -> float:
    if not flights:
        return 0.0
    return min(f.get("price_inr", 0.0) for f in flights)


def _validate_budget_math(draft: dict, flights: list[dict]) -> bool:
    computed = _cheapest_flight_cost(flights)
    for day in draft.get("days", []):
        for slot_name in ("morning", "afternoon", "evening"):
            slot = day.get(slot_name)
            if slot:
                computed += slot.get("cost", 0) or 0
        hotel = day.get("hotel")
        if hotel:
            computed += hotel.get("cost_per_night", 0) or 0

    declared = draft.get("total_cost", 0.0)
    return abs(computed - declared) <= BUDGET_MATH_TOLERANCE_INR


# ── Core build function (pure-ish; one network call) ───────────────────────


async def build_itinerary(
    trip_meta: dict,
    flights: list[dict],
    hotels: list[dict],
    attractions: list[dict],
) -> ItineraryDraft | BuilderError:
    if not flights and not hotels and not attractions:
        return BuilderError(error="No source data available to build an itinerary.", code="NO_SOURCE_DATA")

    system_prompt = _SYSTEM_PROMPT
    user_prompt = _build_user_prompt(trip_meta, flights, hotels, attractions)

    try:
        raw = await _call_llm(system_prompt, user_prompt)
    except Exception as exc:
        logger.exception("ItineraryBuilder LLM call failed")
        return BuilderError(error=f"LLM call failed: {exc}", code="LLM_ERROR")

    text = _strip_fences(raw)
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return BuilderError(error=f"Failed to parse builder JSON: {exc}", code="JSON_PARSE_ERROR")

    violations = _validate_data_scope(parsed, hotels, attractions)
    if violations:
        return BuilderError(error="; ".join(violations), code="DATA_SCOPE_VIOLATION")

    if not _validate_budget_math(parsed, flights):
        return BuilderError(
            error=(
                f"Sum of day costs does not match declared total_cost "
                f"within ₹{BUDGET_MATH_TOLERANCE_INR:.0f}."
            ),
            code="BUDGET_MATH_INCONSISTENT",
        )

    try:
        return ItineraryDraft(**parsed)
    except ValidationError as exc:
        return BuilderError(error=f"Draft failed schema validation: {exc}", code="SCHEMA_INVALID")


# ── Agent wrapper (adds DB logging) ─────────────────────────────────────────


class ItineraryBuilder:
    """Thin wrapper: runs build_itinerary() and writes one agent_runs row."""

    async def run(
        self,
        trip_meta: dict,
        flights: list[dict],
        hotels: list[dict],
        attractions: list[dict],
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
    ) -> dict:
        async with timed_run() as timer:
            result = await build_itinerary(trip_meta, flights, hotels, attractions)

        if isinstance(result, BuilderError):
            output = {"draft": None, "error": result.model_dump()}
            status = "failed"
        else:
            output = {"draft": result.model_dump(), "error": None}
            status = "completed"

        if db is not None and trip_id is not None:
            await log_agent_run(
                db=db,
                trip_id=trip_id,
                agent_name="itinerary_builder",
                input={
                    "trip_meta": trip_meta,
                    "flights_count": len(flights),
                    "hotels_count": len(hotels),
                    "attractions_count": len(attractions),
                },
                output=output,
                duration_ms=timer.duration_ms,
                status=status,
            )

        return output
