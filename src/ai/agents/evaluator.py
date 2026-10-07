"""Evaluator Agent — Phase 11: Self-checking before itinerary finalization.

Intended to run after ItineraryBuilder (Phase 12) produces a draft itinerary
and before that draft is persisted / shown to the user. Catches five
categories of correctness failure:

  1. date_out_of_range     — a day in the itinerary falls outside the trip's
                              travel window (trip.start_date .. trip.end_date)
     missing_days          — a date in that window has no day in the itinerary,
                              or has two (DECISIONS.md #93)
  2. budget_mismatch       — itinerary total_cost falls outside the range of
                              the estimate recomputed from source prices
                              (itinerary_estimate: the estimate_budget
                              arithmetic applied to this itinerary — Phase 21:
                              ±10–20% by season, 5% when no range is given),
                              or a hotel is priced in the plan at something
                              other than what the hotel search found
  3. duplicate_activity    — the same activity appears twice on the same day
  4. hallucinated_activity — an activity name that wasn't in ActivitiesAgent's
                              get_attractions results
  5. unbalanced_group      — Phase 25, a group trip: two days of the plan have
                              no stop for one of the travellers, though a place
                              that suits them was found and is not in the plan
                              (src/ai/group.py — the builder repairs this itself,
                              so a failure here means the repair was bypassed)

Design decision (DECISIONS.md #21): all the checks are pure, deterministic
functions — not an LLM call. These are objectively verifiable conditions
(date comparison, arithmetic within a tolerance, set membership, duplicate
detection); an LLM adds cost, latency, and non-determinism for zero benefit.
This mirrors the Phase 7 router and Phase 10 make_budget_decision precedent
already in this codebase. Claude Haiku 4.5 remains reserved for genuinely
subjective quality judgments (see Phase 33's eval-suite grader in the roadmap).

Retry loop:
  - next_agent_for_failures() maps failure types to the sub-agent whose
    output should be regenerated:
        date_out_of_range, duplicate_activity, hallucinated_activity,
        unbalanced_group
            → "activities_agent"
        budget_mismatch
            → "flight_agent"   (budget_mismatch takes priority if present —
                                 fixing cost upstream is cheaper than
                                 re-deriving activities twice)
  - MAX_EVALUATOR_RETRIES = 3. route_after_evaluation() returns "failed"
    once retry_count reaches the cap, regardless of remaining failures —
    same hard-cap pattern as Phase 10's replan_attempts.

Wired into the graph by orchestrator.evaluate_node (Phase 12).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai import group, pricing
from src.ai.itinerary import FREE_TIME, SLOTS
from src.ai.utils.run_logger import log_agent_run, timed_run

MAX_EVALUATOR_RETRIES = 3
BUDGET_TOLERANCE_PCT = 0.05  # 5% — the range around the recomputed total when no estimate range is given
# A hotel priced in the plan must be the price the search found: within ₹50, or 1% of it.
PRICE_TOLERANCE_INR, PRICE_TOLERANCE_PCT = 50.0, 0.01

# Free time is the one activity the builder may write that is not in the
# attraction list — it must not be flagged as a hallucination or a duplicate.
_ALLOWED_FALLBACK_PHRASES: frozenset[str] = frozenset({FREE_TIME})


# ── Models ───────────────────────────────────────────────────────────────


class EvaluatorFailure(BaseModel):
    check: Literal[
        "date_out_of_range",
        "missing_days",
        "budget_mismatch",
        "duplicate_activity",
        "hallucinated_activity",
        "unbalanced_group",
    ]
    detail: str


class EvaluatorVerdict(BaseModel):
    passed: bool
    failures: list[EvaluatorFailure]
    retry_count: int


# ── Draft itinerary walker (matches Phase 12's documented JSON schema) ────


def _iter_slots(draft: dict):
    """Yield (day_number, slot_name, slot_dict) for every populated
    morning/afternoon/evening slot in a draft itinerary.

    Expected shape (Phase 12 schema):
        {"days": [{"day": 1, "date": "...",
                    "morning": {"activity": "...", "cost": 0, ...},
                    "afternoon": {...}, "evening": {...},
                    "hotel": {...}, "flight": null}],
         "total_cost": ..., "currency": "INR"}
    """
    for day in draft.get("days", []):
        day_num = day.get("day")
        for slot_name in SLOTS:
            slot = day.get(slot_name)
            if slot and slot.get("activity"):
                yield day_num, slot_name, slot


# ── Check 1: activity dates within travel window ───────────────────────────


def check_activity_dates(draft: dict, trip_start: str, trip_end: str) -> EvaluatorFailure | None:
    try:
        start = date.fromisoformat(trip_start)
        end = date.fromisoformat(trip_end)
    except (ValueError, TypeError):
        return None  # can't validate without valid trip dates

    bad_days = []
    for day in draft.get("days", []):
        day_date_str = day.get("date")
        if not day_date_str:
            continue
        try:
            day_date = date.fromisoformat(day_date_str)
        except ValueError:
            bad_days.append(day_date_str)
            continue
        if day_date < start or day_date > end:
            bad_days.append(day_date_str)

    if bad_days:
        return EvaluatorFailure(
            check="date_out_of_range",
            detail=f"Day(s) {bad_days} fall outside the trip window {trip_start} to {trip_end}.",
        )
    return None


# ── Check 1b: one entry per day of the trip ────────────────────────────────


def check_day_coverage(draft: dict, trip_start: str, trip_end: str) -> EvaluatorFailure | None:
    """Every date from start to end has exactly one day in the plan.

    A year-long trip once came back as a single day and was saved as "planned".
    """
    try:
        start = date.fromisoformat(trip_start)
        end = date.fromisoformat(trip_end)
    except (ValueError, TypeError):
        return None  # can't validate without valid trip dates

    expected = [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]
    planned = [day.get("date") for day in draft.get("days", [])]
    missing = [d for d in expected if d not in planned]
    repeated = sorted({d for d in planned if d and planned.count(d) > 1})
    if not missing and not repeated:
        return None

    problems = []
    if missing:
        shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
        problems.append(f"no plan for {shown}")
    if repeated:
        problems.append(f"more than one entry for {', '.join(repeated)}")
    return EvaluatorFailure(
        check="missing_days",
        detail=f"The trip is {len(expected)} days ({trip_start} to {trip_end}) but the itinerary has {'; '.join(problems)}.",
    )


# ── Check 2: budget consistency — within the estimate's range ──────────────


def itinerary_estimate(
    draft: dict,
    flights: list[dict],
    hotels: list[dict],
    destination: str | None = None,
    month: int | None = None,
) -> pricing.Estimate:
    """What the draft should cost, recomputed from SOURCE prices, with its range (Phase 21).

    The estimate_budget arithmetic (flights + hotel nights + daily spend)
    applied to this itinerary: the cheapest flight, each hotel night at the
    price the hotel search found, and the activities as drafted — they have no
    source price. The trip's destination and month set how wide the range is.
    """
    hotel_prices = {h.get("name"): h.get("price_per_night_inr") for h in hotels}
    flight = min((f.get("price_inr") or 0.0 for f in flights), default=0.0)
    stay, nights, activities = 0.0, 0, 0.0
    days = draft.get("days", [])
    for day in days:
        if hotel := day.get("hotel") or {}:
            stay += hotel_prices.get(hotel.get("name")) or hotel.get("cost_per_night") or 0.0
            nights += 1
        activities += sum((day.get(slot) or {}).get("cost") or 0.0 for slot in SLOTS)
    return pricing.estimate(
        flights=flight,
        nightly=stay / nights if nights else 0.0,
        nights=nights,
        daily=activities / len(days) if days else 0.0,
        days=len(days),
        destination=destination,
        month=month,
    )


def expected_total_cost(draft: dict, flights: list[dict], hotels: list[dict]) -> float:
    """The recomputed total alone (see itinerary_estimate)."""
    return itinerary_estimate(draft, flights, hotels).total


def check_budget_consistency(
    draft: dict, expected_total: float, budget_range: tuple[float, float] | None = None
) -> EvaluatorFailure | None:
    """The plan's total must fall within the range of the estimate (5% either side when none is given)."""
    total_cost = draft.get("total_cost")
    if total_cost is None or expected_total is None or expected_total == 0:
        return None

    low, high = budget_range or (
        expected_total * (1 - BUDGET_TOLERANCE_PCT),
        expected_total * (1 + BUDGET_TOLERANCE_PCT),
    )
    if low <= total_cost <= high:
        return None
    off = (total_cost - expected_total) / expected_total
    return EvaluatorFailure(
        check="budget_mismatch",
        detail=(
            f"Itinerary total_cost ₹{total_cost:,.0f} is {off:+.1%} off the recomputed total ₹{expected_total:,.0f} — "
            f"outside the estimate's range ₹{low:,.0f}–₹{high:,.0f}."
        ),
    )


def check_source_prices(draft: dict, hotels: list[dict]) -> EvaluatorFailure | None:
    """Every hotel in the plan at the price the hotel search found (Phase 21).

    The range on the total (above) is as wide as the season makes prices
    move; a plan that quotes its hotel at the wrong price must not hide in it.
    """
    found = {h.get("name"): h.get("price_per_night_inr") for h in hotels if h.get("price_per_night_inr")}
    misquoted: dict[str, tuple[float, float]] = {}
    for day in draft.get("days", []):
        hotel = day.get("hotel") or {}
        source, quoted = found.get(hotel.get("name")), hotel.get("cost_per_night")
        if source is None or quoted is None:
            continue
        if abs(quoted - source) > max(PRICE_TOLERANCE_INR, source * PRICE_TOLERANCE_PCT):
            misquoted[hotel["name"]] = (quoted, source)
    if not misquoted:
        return None
    return EvaluatorFailure(
        check="budget_mismatch",
        detail="; ".join(
            f"{name} is priced at ₹{quoted:,.0f} a night in the plan, but the hotel search found ₹{source:,.0f}"
            for name, (quoted, source) in misquoted.items()
        )
        + ".",
    )


# ── Check 3: duplicate activities on the same day ──────────────────────────


def check_duplicate_activities(draft: dict) -> EvaluatorFailure | None:
    for day in draft.get("days", []):
        seen: set[str] = set()
        dupes: set[str] = set()
        for slot_name in SLOTS:
            slot = day.get(slot_name)
            if slot and slot.get("activity"):
                name = slot["activity"]
                if name in seen and name not in _ALLOWED_FALLBACK_PHRASES:  # free time may fill several slots
                    dupes.add(name)
                seen.add(name)
        if dupes:
            return EvaluatorFailure(
                check="duplicate_activity",
                detail=f"Day {day.get('day')} repeats activity/activities: {sorted(dupes)}.",
            )
    return None


# ── Check 4: hallucination — every activity must exist in source data ─────


def check_hallucinated_activities(draft: dict, attractions: list[dict]) -> EvaluatorFailure | None:
    # Build the set of names the builder is allowed to reference:
    #   (a) names actually returned by get_attractions
    #   (b) the builder's free-time slot
    known_names = {a.get("name") for a in attractions if a.get("name")} | _ALLOWED_FALLBACK_PHRASES
    hallucinated = []
    for _day_num, _slot_name, slot in _iter_slots(draft):
        name = slot.get("activity")
        if name and name not in known_names:
            hallucinated.append(name)

    if hallucinated:
        return EvaluatorFailure(
            check="hallucinated_activity",
            detail=f"Activities not present in get_attractions results: {sorted(set(hallucinated))}.",
        )
    return None


# ── Check 5: a group trip gives everyone something (Phase 25) ─────────────


def check_group_balance(
    draft: dict, attractions: list[dict], group_members: list[dict] | None
) -> EvaluatorFailure | None:
    """In every two days, a stop for each traveller — as far as the places found allow.

    A traveller nothing was found for, or whose places are all in the plan
    already, is not a failure: nothing here could do better. A failure is a gap
    that an unused place would fill.
    """
    if not group.is_group(group_members):
        return None
    gaps = group.fixable_gaps(draft, attractions, group_members)
    if not gaps:
        return None
    return EvaluatorFailure(
        check="unbalanced_group",
        detail=f"Someone in the group is left out of two days though a place for them was found: {'; '.join(gaps[:3])}.",
    )


# ── Combined pure evaluation ────────────────────────────────────────────────


def evaluate_itinerary(
    draft: dict,
    trip_start: str,
    trip_end: str,
    expected_budget_total: float,
    attractions: list[dict],
    retry_count: int = 0,
    budget_range: tuple[float, float] | None = None,
    hotels: list[dict] | None = None,
    group_members: list[dict] | None = None,
) -> EvaluatorVerdict:
    """Run every check and return a single EvaluatorVerdict.

    Pure function — no I/O, no mocks needed in tests. All failures found
    are reported, not just the first (helps the caller pick the best
    single agent to retry via next_agent_for_failures()).

    Phase 21: `budget_range` is the estimate's (total_min, total_max) — the
    total must fall inside it; `hotels` (the search results) has each hotel's
    price in the plan checked against what was found.
    """
    failures: list[EvaluatorFailure] = []

    for failure in (
        check_activity_dates(draft, trip_start, trip_end),
        check_day_coverage(draft, trip_start, trip_end),
        check_budget_consistency(draft, expected_budget_total, budget_range),
        check_source_prices(draft, hotels or []),
        check_duplicate_activities(draft),
        check_hallucinated_activities(draft, attractions),
        check_group_balance(draft, attractions, group_members),
    ):
        if failure is not None:
            failures.append(failure)

    return EvaluatorVerdict(passed=len(failures) == 0, failures=failures, retry_count=retry_count)


# ── Retry routing ────────────────────────────────────────────────────────


_FAILURE_TO_AGENT: dict[str, str] = {
    "date_out_of_range": "activities_agent",
    "missing_days": "activities_agent",
    "duplicate_activity": "activities_agent",
    "hallucinated_activity": "activities_agent",
    "unbalanced_group": "activities_agent",
    "budget_mismatch": "flight_agent",
}


def next_agent_for_failures(failures: list[EvaluatorFailure]) -> str | None:
    """Return the sub-agent name whose output should be regenerated first.

    Priority: budget_mismatch (cheapest to fix, affects everything
    downstream) wins if present; otherwise the first failure's mapped agent.
    Returns None if there are no failures.
    """
    if not failures:
        return None
    checks = [f.check for f in failures]
    if "budget_mismatch" in checks:
        return _FAILURE_TO_AGENT["budget_mismatch"]
    return _FAILURE_TO_AGENT[checks[0]]


def route_after_evaluation(verdict: EvaluatorVerdict) -> str:
    """Conditional-edge style router. Returns "passed" | "retry" | "failed".

    "failed" fires once retry_count has reached MAX_EVALUATOR_RETRIES,
    regardless of how many failures remain — hard cap, same pattern as
    Phase 10's replan_attempts cap.
    """
    if verdict.passed:
        return "passed"
    if verdict.retry_count >= MAX_EVALUATOR_RETRIES:
        return "failed"
    return "retry"


# ── Agent wrapper (adds DB logging — Dev B) ────────────────────────────────


class EvaluatorAgent:
    """Thin wrapper: runs evaluate_itinerary() and writes one agent_runs row."""

    async def run(
        self,
        draft: dict,
        trip_start: str,
        trip_end: str,
        expected_budget_total: float,
        attractions: list[dict],
        retry_count: int = 0,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
        turn: int = 1,
        budget_range: tuple[float, float] | None = None,
        hotels: list[dict] | None = None,
        group_members: list[dict] | None = None,
    ) -> EvaluatorVerdict:
        """Phase 15: `turn` parameter forwarded to log_agent_run. Defaults to 1."""
        async with timed_run() as timer:
            verdict = evaluate_itinerary(
                draft=draft,
                trip_start=trip_start,
                trip_end=trip_end,
                expected_budget_total=expected_budget_total,
                attractions=attractions,
                retry_count=retry_count,
                budget_range=budget_range,
                hotels=hotels,
                group_members=group_members,
            )

        if db is not None and trip_id is not None:
            await log_agent_run(
                db=db,
                trip_id=trip_id,
                agent_name="evaluator",
                input={
                    "trip_start": trip_start,
                    "trip_end": trip_end,
                    "expected_budget_total": expected_budget_total,
                    "budget_range": list(budget_range) if budget_range else None,
                    "attractions_count": len(attractions),
                    "retry_count": retry_count,
                },
                output=verdict.model_dump(),
                duration_ms=timer.duration_ms,
                status="completed" if verdict.passed else "failed",
                turn=turn,
            )

        return verdict
