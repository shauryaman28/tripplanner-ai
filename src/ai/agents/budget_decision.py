"""Budget conflict detection and re-planning logic — Phase 10.

BudgetDecisionNode runs after FlightAgent returns and before HotelAgent
is called. It evaluates whether the remaining budget is viable.

Thresholds (fraction of total budget remaining after cheapest flight):
    ≥ 50%  → "continue"  — comfortable, proceed with hotel + activities
    35–49% → "replan"    — borderline, search for cheaper flights (≤ 2 attempts)
    < 35%  → "escalate"  — too tight, inform user and offer alternatives
    replan_attempts ≥ MAX_REPLAN_ATTEMPTS → always escalate

Design: make_budget_decision() is a pure function — no I/O, no side effects,
trivially unit-testable. The LangGraph node wraps it with DB logging.

Verification:
    ₹40k budget, ₹28k flights (70%) → 30% remaining < 35% → escalate  ✓
    ₹40k budget, ₹16k flights (40%) → 60% remaining ≥ 50% → continue  ✓
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# ── Thresholds ─────────────────────────────────────────────────────────────

ESCALATE_THRESHOLD = 0.35    # remaining < 35% → escalate
REPLAN_THRESHOLD = 0.50      # remaining < 50% → replan
MAX_REPLAN_ATTEMPTS = 2      # hard cap on re-plan loops


# ── Model ──────────────────────────────────────────────────────────────────


class BudgetDecision(BaseModel):
    decision: Literal["continue", "replan", "escalate"]
    reason: str
    remaining_budget: float
    flight_cost: float
    total_budget: float


# ── Core logic (pure function) ─────────────────────────────────────────────


def make_budget_decision(
    flights: list[dict],
    total_budget: float,
    replan_attempts: int = 0,
) -> BudgetDecision:
    """Return a BudgetDecision based on cheapest available flight vs. budget.

    Pure function — no I/O, no mocks needed in unit tests.
    """
    if not flights or total_budget <= 0:
        return BudgetDecision(
            decision="escalate",
            reason="No flights found or zero budget — cannot proceed with planning.",
            remaining_budget=0.0,
            flight_cost=0.0,
            total_budget=total_budget,
        )

    flight_cost = min(f.get("price_inr", float("inf")) for f in flights)
    remaining = total_budget - flight_cost
    remaining_pct = remaining / total_budget if total_budget else 0.0

    # Hard cap takes priority over percentage check
    if replan_attempts >= MAX_REPLAN_ATTEMPTS:
        return BudgetDecision(
            decision="escalate",
            reason=(
                f"Flights cost ₹{flight_cost:,.0f} (₹{remaining:,.0f} = {remaining_pct:.0%} remaining). "
                f"Maximum re-planning attempts ({MAX_REPLAN_ATTEMPTS}) reached."
            ),
            remaining_budget=remaining,
            flight_cost=flight_cost,
            total_budget=total_budget,
        )

    if remaining_pct < ESCALATE_THRESHOLD:
        return BudgetDecision(
            decision="escalate",
            reason=(
                f"Flights cost ₹{flight_cost:,.0f} leaving only ₹{remaining:,.0f} "
                f"({remaining_pct:.0%} of budget) — less than the {ESCALATE_THRESHOLD:.0%} "
                f"minimum needed for hotels and activities."
            ),
            remaining_budget=remaining,
            flight_cost=flight_cost,
            total_budget=total_budget,
        )

    if remaining_pct < REPLAN_THRESHOLD:
        return BudgetDecision(
            decision="replan",
            reason=(
                f"Flights cost ₹{flight_cost:,.0f} leaving ₹{remaining:,.0f} "
                f"({remaining_pct:.0%} of budget). "
                f"Searching for cheaper options "
                f"(attempt {replan_attempts + 1}/{MAX_REPLAN_ATTEMPTS})."
            ),
            remaining_budget=remaining,
            flight_cost=flight_cost,
            total_budget=total_budget,
        )

    return BudgetDecision(
        decision="continue",
        reason=(
            f"Budget check passed. Flights ₹{flight_cost:,.0f}; "
            f"₹{remaining:,.0f} ({remaining_pct:.0%}) remaining for hotels and activities."
        ),
        remaining_budget=remaining,
        flight_cost=flight_cost,
        total_budget=total_budget,
    )


def replan_flight_budget(original_budget: float, attempt: int) -> float:
    """Return a reduced budget cap for re-plan attempt N.

    Attempt 1: 65% of original (find connecting/budget flights)
    Attempt 2: 55% of original (last resort before escalate)
    """
    factors = {1: 0.65, 2: 0.55}
    return original_budget * factors.get(attempt, 0.60)
