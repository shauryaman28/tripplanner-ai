"""Ways out of a budget conflict, each with what it would cost — Phase 21.

When the flights leave too little of the budget, the planner stops and asks
(Phase 10, budget_decision.py). This works out what to offer, with the
estimate_budget arithmetic (src/ai/pricing.py) applied to what the run already
knows — the fare found, the budget, the dates, how many are travelling:

    the trip as asked    real flights, a 4-star stay and things to do there
    a cheaper stay       a 3-star hotel — or a budget one, when only that fits
    a shorter trip       the longest one that fits the budget
    the off-season       the same trip in the destination's cheapest month

Nothing is searched for any of it. A stay and things to do are typical prices
for the destination in that month — said as "about" — and the fare in
another month is today's fare moved by the two months' seasons. Two plain
actions are offered beside them: search connecting flights (unless a re-plan
already did), and raise the budget to what these flights need.

Pure: no I/O. The options travel in the `budget_conflict` event and come back
from GET /status; POST /trips/{id}/replan applies the one picked.
"""

from __future__ import annotations

from datetime import date

from src.ai import pricing
from src.ai.agents.budget_decision import viable_budget

AS_ASKED = "mid-range"  # the stay a trip "as asked" is priced with
CHEAPER_STAYS = ("standard", "budget")  # tried in turn: the first that fits is offered


def _within(total: int, budget: float) -> str:
    over = total - budget
    return "within budget" if over <= 0 else f"₹{pricing.round_inr(over):,} over budget"


def _trip(flight_cost: float, days: int, travellers: int, destination: str, month: int, tier: str) -> pricing.Estimate:
    return pricing.typical_trip(
        flight_cost=flight_cost, days=days, travellers=travellers, destination=destination, month=month, tier=tier
    )


def conflict_alternatives(
    *,
    flight_cost: float,
    budget: float,
    start: date,
    end: date,
    travellers: int,
    destination: str,
    replan_attempts: int,
    today: date,
) -> tuple[dict, list[dict]]:
    """(the trip as asked, the options) for a budget conflict. Amounts are in whole rupees, rounded to ₹100."""
    days = (end - start).days + 1
    travellers = max(1, travellers)
    asked = _trip(flight_cost, days, travellers, destination, start.month, AS_ASKED)
    asked_total = pricing.round_inr(asked.total)
    season = asked.season
    trip = {
        "total": asked_total,
        "total_min": pricing.round_inr(asked.total_min),
        "total_max": pricing.round_inr(asked.total_max),
        "stay": pricing.STAY_WORDS[AS_ASKED],
        "month": start.month,
        "season": season.label,
        "multiplier": season.multiplier,
        "about": season.describe(destination),
        "budget": round(budget),
    }

    options: list[dict] = []

    # 1. A cheaper stay: a 3-star hotel, or a budget one when only that fits.
    for tier in CHEAPER_STAYS:
        cheaper = _trip(flight_cost, days, travellers, destination, start.month, tier)
        total = pricing.round_inr(cheaper.total)
        if total <= budget or tier == CHEAPER_STAYS[-1]:
            nightly = pricing.round_inr(pricing.typical_nightly(season, tier, travellers))
            options.append(
                {
                    "choice": "cheaper_hotel",
                    "description": f"Stay at {pricing.STAY_WORDS[tier]}",
                    "estimated_saving": (
                        f"About ₹{asked_total - total:,} less — ₹{total:,} in all, {_within(total, budget)}"
                    ),
                    "saving": asked_total - total,
                    "total": total,
                    "fits": total <= budget,
                    "tier": tier,
                    "nightly": nightly,
                }
            )
            break

    # 2. A shorter trip, as comfortable as asked: the longest that fits (one night at least).
    for shorter_days in range(days - 1, 1, -1):
        shorter = _trip(flight_cost, shorter_days, travellers, destination, start.month, AS_ASKED)
        total = pricing.round_inr(shorter.total)
        if total <= budget:
            options.append(
                {
                    "choice": "reduce_days",
                    "description": f"Make it {shorter_days} days instead of {days}",
                    "estimated_saving": f"About ₹{asked_total - total:,} less — ₹{total:,} in all, within budget",
                    "saving": asked_total - total,
                    "total": total,
                    "fits": True,
                    "days": shorter_days,
                }
            )
            break

    # 3. The off-season: the same trip in the destination's cheapest month, with the fare moved by the seasons.
    if (moved := pricing.nearest_off_peak(destination, start, today)) is not None:
        off_season = pricing.season_for(destination, moved.month)
        fare = flight_cost * off_season.multiplier / season.multiplier
        later = _trip(fare, days, travellers, destination, moved.month, AS_ASKED)
        total = pricing.round_inr(later.total)
        month = pricing.MONTH_NAMES[moved.month - 1]
        options.append(
            {
                "choice": "off_peak",
                "description": f"Go in {month} instead — {pricing.off_season_words(off_season)}",
                "estimated_saving": (
                    f"Flights about ₹{pricing.round_inr(flight_cost - fare):,} less — "
                    f"₹{total:,} in all, {_within(total, budget)}"
                ),
                "saving": asked_total - total,
                "flight_saving": pricing.round_inr(flight_cost - fare),
                "total": total,
                "fits": total <= budget,
                "month": moved.month,
                "start_date": moved.isoformat(),
                "end_date": (moved + (end - start)).isoformat(),
            }
        )

    return trip, options + plain_options(flight_cost, budget, replan_attempts)


def plain_options(flight_cost: float, budget: float, replan_attempts: int) -> list[dict]:
    """The two ways out that need no estimate: other flights, or more money."""
    options = []
    if replan_attempts == 0:  # a re-plan has searched connections already: offering it again is a dead end
        options.append(
            {
                "choice": "cheaper_flights",
                "description": "Search for cheaper connecting flights",
                "estimated_saving": "Flights with up to two stops, searched again before anything is planned",
                "saving": None,
            }
        )
    target = max(budget * 1.25, viable_budget(flight_cost))  # clears the check outright, and never less than +25%
    options.append(
        {
            "choice": "increase_budget",
            "description": f"Increase total budget to ₹{target:,.0f}",
            "estimated_saving": f"Additional ₹{target - budget:,.0f}",
            "saving": None,
            "budget": target,
        }
    )
    return options
