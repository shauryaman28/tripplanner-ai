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

Phase 21: when this rule would stop a trip, the node prices the rest of it
first (budget_alternatives.py, orchestrator._settle_conflict): a trip that fits
at typical prices goes ahead, a way out the traveller already picked is gone
ahead with, and otherwise the conflict carries its ways out with amounts.

Verification:
    ₹40k budget, ₹28k flights (70%) → 30% remaining < 35% → escalate  ✓
    ₹40k budget, ₹16k flights (40%) → 60% remaining ≥ 50% → continue  ✓
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel

# ── Thresholds ─────────────────────────────────────────────────────────────

ESCALATE_THRESHOLD = 0.35  # remaining < 35% → escalate
REPLAN_THRESHOLD = 0.50  # remaining < 50% → replan
MAX_REPLAN_ATTEMPTS = 2  # hard cap on re-plan loops

# Flight-search failures that really are about money. Anything else (provider
# not configured, API down, no airport for the destination) is a partial
# failure: planning continues without flights instead of failing the trip.
_BUDGET_ERROR_CODES = {"NO_RESULTS", "BUDGET_TOO_LOW"}


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
    flight_error: dict | None = None,
) -> BudgetDecision:
    """Return a BudgetDecision based on cheapest available flight vs. budget.

    Pure function — no I/O, no mocks needed in unit tests.
    """
    if flight_error and flight_error.get("code") not in _BUDGET_ERROR_CODES and total_budget > 0:
        return BudgetDecision(
            decision="continue",
            reason=(
                f"Flight search unavailable ({flight_error.get('code', 'UNKNOWN')}) — "
                "planning hotels and activities without flights."
            ),
            remaining_budget=total_budget,
            flight_cost=0.0,
            total_budget=total_budget,
        )

    if not flights or total_budget <= 0:
        return BudgetDecision(
            decision="escalate",
            reason="No flights found within the budget — cannot proceed with planning.",
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


def viable_budget(flight_cost: float) -> float:
    """Smallest total budget (rounded up to ₹500) at which this flight cost passes the check outright.

    It is what "increase the budget" has to reach: a fixed +25% is a dead end
    when the flights alone are most of the budget.
    """
    return math.ceil(flight_cost / (1 - REPLAN_THRESHOLD) / 500) * 500


def replan_flight_budget(original_budget: float, attempt: int) -> float:
    """Return a reduced budget cap for re-plan attempt N.

    Attempt 1: 65% of original (find connecting/budget flights)
    Attempt 2: 55% of original (last resort before escalate)
    """
    factors = {1: 0.65, 2: 0.55}
    return original_budget * factors.get(attempt, 0.60)
