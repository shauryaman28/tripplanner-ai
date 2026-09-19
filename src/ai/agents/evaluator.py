"""Evaluator Agent — Phase 11: Self-checking before itinerary finalization.

Intended to run after ItineraryBuilder (Phase 12) produces a draft itinerary
and before that draft is persisted / shown to the user. Catches four
categories of correctness failure:

  1. date_out_of_range     — a day in the itinerary falls outside the trip's
                              travel window (trip.start_date .. trip.end_date)
  2. budget_mismatch       — itinerary total_cost differs from the
                              estimate_budget total by more than 5%
  3. duplicate_activity    — the same activity appears twice on the same day
  4. hallucinated_activity — an activity name that wasn't in ActivitiesAgent's
                              get_attractions results

Design decision (DECISIONS.md #21): all four checks are pure, deterministic
functions — not an LLM call. These are objectively verifiable conditions
(date comparison, arithmetic within a tolerance, set membership, duplicate
detection); an LLM adds cost, latency, and non-determinism for zero benefit.
This mirrors the Phase 7 router and Phase 10 make_budget_decision precedent
already in this codebase. Claude Haiku 4.5 remains reserved for genuinely
subjective quality judgments (see Phase 33's eval-suite grader in the roadmap).

Retry loop:
  - next_agent_for_failures() maps failure types to the sub-agent whose
    output should be regenerated:
        date_out_of_range, duplicate_activity, hallucinated_activity
            → "activities_agent"
        budget_mismatch
            → "flight_agent"   (budget_mismatch takes priority if present —
                                 fixing cost upstream is cheaper than
                                 re-deriving activities twice)
  - MAX_EVALUATOR_RETRIES = 3. route_after_evaluation() returns "failed"
    once retry_count reaches the cap, regardless of remaining failures —
    same hard-cap pattern as Phase 10's replan_attempts.

Scope note: this module is standalone and fully unit-tested against the
Phase 12 draft-itinerary schema. It is not yet wired into orchestrator.py —
there is no ItineraryBuilder node for it to sit after until Phase 12 exists.
Wiring happens there.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai.utils.run_logger import log_agent_run, timed_run

MAX_EVALUATOR_RETRIES = 3
BUDGET_TOLERANCE_PCT = 0.05  # 5%

# Phrases the ItineraryBuilder is documented to write when source data is
# missing (see builder.py _ALLOWED_FALLBACK_PHRASES and itinerary_builder_v1.md).
# These must NOT be flagged as hallucinations — they are intentional fallbacks.
_ALLOWED_FALLBACK_PHRASES: frozenset[str] = frozenset({"Explore the area"})


# ── Models ───────────────────────────────────────────────────────────────


class EvaluatorFailure(BaseModel):
    check: Literal[
        "date_out_of_range",
        "budget_mismatch",
        "duplicate_activity",
        "hallucinated_activity",
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
        for slot_name in ("morning", "afternoon", "evening"):
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


# ── Check 2: budget consistency within 5% ──────────────────────────────────


def check_budget_consistency(draft: dict, expected_total: float) -> EvaluatorFailure | None:
    total_cost = draft.get("total_cost")
    if total_cost is None or expected_total is None or expected_total == 0:
        return None

    diff_pct = abs(total_cost - expected_total) / expected_total
    if diff_pct > BUDGET_TOLERANCE_PCT:
        return EvaluatorFailure(
            check="budget_mismatch",
            detail=(
                f"Itinerary total_cost ₹{total_cost:,.0f} differs from estimate_budget total "
                f"₹{expected_total:,.0f} by {diff_pct:.1%} — exceeds the {BUDGET_TOLERANCE_PCT:.0%} tolerance."
            ),
        )
    return None


# ── Check 3: duplicate activities on the same day ──────────────────────────


def check_duplicate_activities(draft: dict) -> EvaluatorFailure | None:
    for day in draft.get("days", []):
        seen: set[str] = set()
        dupes: set[str] = set()
        for slot_name in ("morning", "afternoon", "evening"):
            slot = day.get(slot_name)
            if slot and slot.get("activity"):
                name = slot["activity"]
                if name in seen:
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
    #   (b) documented fallback phrases the builder writes when attractions
    #       are empty for a slot (e.g. "Explore the area" from builder.py)
    known_names = (
        {a.get("name") for a in attractions if a.get("name")}
        | _ALLOWED_FALLBACK_PHRASES
    )
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


# ── Combined pure evaluation ────────────────────────────────────────────────


def evaluate_itinerary(
    draft: dict,
    trip_start: str,
    trip_end: str,
    expected_budget_total: float,
    attractions: list[dict],
    retry_count: int = 0,
) -> EvaluatorVerdict:
    """Run all four checks and return a single EvaluatorVerdict.

    Pure function — no I/O, no mocks needed in tests. All failures found
    are reported, not just the first (helps the caller pick the best
    single agent to retry via next_agent_for_failures()).
    """
    failures: list[EvaluatorFailure] = []

    for failure in (
        check_activity_dates(draft, trip_start, trip_end),
        check_budget_consistency(draft, expected_budget_total),
        check_duplicate_activities(draft),
        check_hallucinated_activities(draft, attractions),
    ):
        if failure is not None:
            failures.append(failure)

    return EvaluatorVerdict(passed=len(failures) == 0, failures=failures, retry_count=retry_count)


# ── Retry routing ────────────────────────────────────────────────────────


_FAILURE_TO_AGENT: dict[str, str] = {
    "date_out_of_range": "activities_agent",
    "duplicate_activity": "activities_agent",
    "hallucinated_activity": "activities_agent",
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
                    "attractions_count": len(attractions),
                    "retry_count": retry_count,
                },
                output=verdict.model_dump(),
                duration_ms=timer.duration_ms,
                status="completed" if verdict.passed else "failed",
                turn=turn,
            )

        return verdict
