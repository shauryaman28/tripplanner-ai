"""Phase 21 — smarter budget intelligence.

  seasons        a destination's months against its cheapest; peak, shoulder, off-peak; the nearest off-season
  estimate       estimate_budget with a destination and a month: a range, the season, the off-season price
  evaluator      the total must fall within the estimate's range; each hotel at the price the search found
  alternatives   the Goa / ₹40,000 conflict priced three ways out — arithmetic only, nothing searched
  budget check   a trip that fits at typical prices is not stopped; a way out the traveller picked is gone ahead with
  routes         POST /trips/{id}/replan applies the option as it was offered; GET /status returns the priced trip

No network: every amount here is arithmetic on what a run already knows.
"""

import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.models.agent_run import AgentRun
from src.ai import pricing
from src.ai.agents.budget_alternatives import conflict_alternatives, plain_options
from src.ai.agents.budget_decision import make_budget_decision
from src.ai.agents.evaluator import evaluate_itinerary, itinerary_estimate
from src.ai.mcp_server.models import BudgetEstimate, BudgetInput
from src.ai.mcp_server.tools import estimate_budget
from src.ai.orchestrator.orchestrator import budget_decision_node, escalate_node
from tests.unit.test_pipeline_regressions import START, _client, _trip

TODAY = date(2026, 10, 3)


# ── Seasons ────────────────────────────────────────────────────────────────


def test_goa_in_december_costs_about_forty_percent_more_than_in_july():
    """The roadmap's own figure: Goa December ≈ 1.4 × Goa July."""
    december, july = pricing.season_for("Goa", 12), pricing.season_for("Goa", 7)
    assert december.multiplier / july.multiplier == pytest.approx(1.4)
    assert (december.label, july.label) == ("peak", "off-peak")
    assert december.off_peak_months == (6, 7, 8, 9)
    assert december.describe("Goa") == "December is peak season in Goa: prices run about 40% above the off-season."


@pytest.mark.parametrize(
    ("destination", "profile"),
    [
        ("North Goa", "coast"),
        ("Udaipur", "heritage"),
        ("Shimla", "hills"),
        ("Leh", "himalaya"),
        ("Kedarnath", "himalaya"),  # open from May, shut in winter: not December's holiday rush
        ("Patna", "india"),
        ("", "india"),
        ("वाराणसी", "india"),  # not matched by name: India-wide seasons
    ],
)
def test_a_destination_follows_the_seasons_of_its_kind(destination, profile):
    assert pricing.profile_for(destination) == profile


def test_the_busier_the_season_the_further_a_price_may_move():
    assert pricing.season_for("Goa", 12).volatility == 0.20  # peak
    assert pricing.season_for("Goa", 2).volatility == 0.15  # shoulder
    assert pricing.season_for("Goa", 7).volatility == 0.10  # off-peak


@pytest.mark.parametrize(
    ("destination", "start", "today", "moved"),
    [
        ("Goa", date(2026, 12, 10), TODAY, date(2027, 6, 10)),  # the next monsoon
        ("Shimla", date(2027, 5, 31), TODAY, date(2027, 2, 28)),  # an off-season before the trip; the 31st clamped
        ("Goa", date(2027, 12, 10), date(2027, 5, 20), date(2027, 7, 10)),  # June is too close to book
        ("Goa", date(2027, 7, 1), TODAY, None),  # the trip is in the off-season already
    ],
)
def test_the_nearest_off_season_is_far_enough_ahead_to_book(destination, start, today, moved):
    assert pricing.nearest_off_peak(destination, start, today) == moved


# ── estimate_budget ────────────────────────────────────────────────────────


def test_estimate_budget_returns_a_range_not_just_a_total():
    """Roadmap acceptance: estimate_budget(8000, 3000, 5, 2000, "Goa", 12) returns a range."""
    result = estimate_budget(
        BudgetInput(flights=8_000, hotels=3_000, days=5, daily_spend=2_000, destination="Goa", month=12)
    )
    assert isinstance(result, BudgetEstimate)
    assert result.total == 33_000  # 8,000 + 5 × 3,000 + 5 × 2,000 — the prices are December's
    assert (result.total_min, result.total_max) == (26_400, 39_600)  # ±20%: December is peak season
    assert (result.season, result.season_multiplier, result.off_peak_months) == ("peak", 1.4, [6, 7, 8, 9])
    assert result.off_peak_total == pytest.approx(33_000 / 1.4)  # the same trip in the monsoon
    assert "December is peak season in Goa" in result.notes


def test_estimate_budget_without_a_month_assumes_the_widest_range():
    result = estimate_budget(BudgetInput(flights=8_000, hotels=3_000, days=5, daily_spend=2_000))
    assert (result.total, result.total_min, result.total_max) == (33_000, 26_400, 39_600)
    assert (result.season, result.season_multiplier, result.off_peak_total) == (None, 1.0, None)


def test_estimate_budget_counts_the_nights_it_is_given():
    """A five-day trip has four hotel nights; left out, nights are the days (the tool's Phase 3 contract)."""
    four_nights = estimate_budget(BudgetInput(flights=0, hotels=3_000, days=5, nights=4, daily_spend=0, month=7))
    assert four_nights.hotels == 12_000 and four_nights.total_min == pytest.approx(12_000 * 0.9)


def test_estimate_budget_in_the_off_season_has_no_cheaper_month():
    result = estimate_budget(
        BudgetInput(flights=8_000, hotels=3_000, days=5, daily_spend=0, destination="Goa", month=7)
    )
    assert (result.season, result.off_peak_total) == ("off-peak", None)
    assert (result.total_min, result.total_max) == pytest.approx((20_700, 25_300))  # ±10%


def test_a_month_must_be_a_month():
    with pytest.raises(ValidationError):
        BudgetInput(flights=1, hotels=1, days=1, daily_spend=1, month=13)


# ── Evaluator ──────────────────────────────────────────────────────────────

_ATTRACTIONS = [{"name": "Fort Aguada"}]
_HOTELS = [{"name": "Goa Grand", "price_per_night_inr": 4_500.0}]
_FLIGHTS = [{"price_inr": 8_200.0}]


def _draft(nightly: float = 4_500.0, total: float | None = None) -> dict:
    days = [
        {"day": 1, "date": "2026-12-10", "morning": {"activity": "Fort Aguada", "cost": 500}},
        {"day": 2, "date": "2026-12-11"},
        {"day": 3, "date": "2026-12-12"},
    ]
    for day in days[:2]:
        day["hotel"] = {"name": "Goa Grand", "cost_per_night": nightly}
    return {"days": days, "total_cost": total if total is not None else 8_200 + 2 * nightly + 500}


def test_the_evaluator_rejects_an_itinerary_whose_total_falls_outside_the_range():
    """Roadmap acceptance: the total must fall within [total_min, total_max]."""
    estimate = itinerary_estimate(_draft(), _FLIGHTS, _HOTELS, destination="Goa", month=12)
    assert (estimate.total, estimate.total_min, estimate.total_max) == pytest.approx((17_700, 14_160, 21_240))
    within = evaluate_itinerary(
        _draft(total=20_000), "2026-12-10", "2026-12-12", estimate.total, _ATTRACTIONS,
        budget_range=(estimate.total_min, estimate.total_max), hotels=_HOTELS,
    )  # fmt: skip
    outside = evaluate_itinerary(
        _draft(total=22_000), "2026-12-10", "2026-12-12", estimate.total, _ATTRACTIONS,
        budget_range=(estimate.total_min, estimate.total_max), hotels=_HOTELS,
    )  # fmt: skip
    assert within.passed
    assert [f.check for f in outside.failures] == ["budget_mismatch"]
    assert "outside the estimate's range ₹14,160–₹21,240" in outside.failures[0].detail


def test_the_range_is_as_wide_as_the_season():
    peak = itinerary_estimate(_draft(), _FLIGHTS, _HOTELS, destination="Goa", month=12)
    monsoon = itinerary_estimate(_draft(), _FLIGHTS, _HOTELS, destination="Goa", month=7)
    assert peak.total_max / peak.total == pytest.approx(1.2)
    assert monsoon.total_max / monsoon.total == pytest.approx(1.1)


def test_a_misquoted_hotel_cannot_hide_inside_the_range():
    """₹4,000 a night for a ₹4,500 hotel moves the total by 5.6% — inside a peak season's ±20%."""
    draft = _draft(nightly=4_000.0)
    estimate = itinerary_estimate(draft, _FLIGHTS, _HOTELS, destination="Goa", month=12)
    verdict = evaluate_itinerary(
        draft, "2026-12-10", "2026-12-12", estimate.total, _ATTRACTIONS,
        budget_range=(estimate.total_min, estimate.total_max), hotels=_HOTELS,
    )  # fmt: skip
    assert [f.check for f in verdict.failures] == ["budget_mismatch"]
    assert verdict.failures[0].detail == (
        "Goa Grand is priced at ₹4,000 a night in the plan, but the hotel search found ₹4,500."
    )


def test_a_rounded_hotel_price_is_not_a_misquote():
    draft = _draft(nightly=4_499.5)
    estimate = itinerary_estimate(draft, _FLIGHTS, _HOTELS, destination="Goa", month=12)
    assert evaluate_itinerary(
        draft, "2026-12-10", "2026-12-12", estimate.total, _ATTRACTIONS,
        budget_range=(estimate.total_min, estimate.total_max), hotels=_HOTELS,
    ).passed  # fmt: skip


# ── Alternatives ───────────────────────────────────────────────────────────

# The roadmap's scenario: 5 days in Goa in December, ₹40,000, and flights at ₹28,000.
GOA = {
    "flight_cost": 28_000.0,
    "budget": 40_000.0,
    "start": date(2026, 12, 10),
    "end": date(2026, 12, 14),
    "travellers": 1,
    "destination": "Goa",
    "replan_attempts": 0,
    "today": TODAY,
}


def test_the_goa_conflict_offers_three_ways_out_with_concrete_amounts():
    trip, options = conflict_alternatives(**GOA)
    assert trip == {
        "total": 56_700,  # ₹28,000 + 4 nights at a 4-star (₹6,300 in December) + 5 days at ₹700
        "total_min": 45_400,
        "total_max": 68_000,
        "stay": "a 4-star hotel",
        "month": 12,
        "season": "peak",
        "multiplier": 1.4,
        "about": "December is peak season in Goa: prices run about 40% above the off-season.",
        "budget": 40_000,
    }
    hotel, shorter, off_season, flights, budget = options
    # 1. a 3-star stay is still ₹5,500 over: a budget hotel is the cheaper stay that fits
    assert (hotel["choice"], hotel["tier"], hotel["total"], hotel["saving"], hotel["fits"]) == (
        "cheaper_hotel", "budget", 38_200, 18_500, True,
    )  # fmt: skip
    assert hotel["description"] == "Stay at a budget hotel" and hotel["nightly"] == 1_700
    # 2. the longest trip that fits, as comfortable as asked
    assert (shorter["choice"], shorter["days"], shorter["total"]) == ("reduce_days", 2, 35_700)
    assert shorter["description"] == "Make it 2 days instead of 5"
    # 3. the monsoon: the roadmap's "saves ₹8,000 on flights", ₹500 over in all
    assert (off_season["choice"], off_season["flight_saving"], off_season["total"], off_season["fits"]) == (
        "off_peak", 8_000, 40_500, False,
    )  # fmt: skip
    assert (off_season["start_date"], off_season["end_date"]) == ("2027-06-10", "2027-06-14")
    assert off_season["description"] == "Go in June instead — the monsoon"
    assert off_season["estimated_saving"] == "Flights about ₹8,000 less — ₹40,500 in all, ₹500 over budget"
    # and the two plain actions
    assert flights["choice"] == "cheaper_flights" and flights["saving"] is None
    assert (budget["choice"], budget["budget"]) == ("increase_budget", 56_000)


def test_a_three_star_stay_is_offered_when_it_fits():
    """₹40,000 of flights on ₹60,000: a 3-star stay brings the trip to ₹57,500."""
    _, options = conflict_alternatives(**{**GOA, "budget": 60_000.0, "flight_cost": 40_000.0})
    hotel = options[0]
    assert (hotel["tier"], hotel["description"], hotel["total"], hotel["fits"]) == (
        "standard",
        "Stay at a 3-star hotel",
        57_500,
        True,
    )


def test_no_shorter_trip_is_offered_when_none_fits():
    """Udaipur, ₹12,000 for two, ₹8,200 flights: not even one night at a 4-star fits."""
    _, options = conflict_alternatives(
        flight_cost=8_200.0, budget=12_000.0, start=date(2027, 12, 14), end=date(2027, 12, 16), travellers=2,
        destination="Udaipur", replan_attempts=0, today=TODAY,
    )  # fmt: skip
    assert [o["choice"] for o in options] == ["cheaper_hotel", "off_peak", "cheaper_flights", "increase_budget"]
    hotel = options[0]
    assert (hotel["tier"], hotel["total"], hotel["fits"]) == ("budget", 15_500, False)
    assert hotel["estimated_saving"] == "About ₹8,900 less — ₹15,500 in all, ₹3,500 over budget"


def test_a_two_day_trip_cannot_be_made_shorter_nor_moved_out_of_its_off_season():
    _, options = conflict_alternatives(**{**GOA, "start": date(2027, 7, 1), "end": date(2027, 7, 2)})
    assert "reduce_days" not in [o["choice"] for o in options]
    assert "off_peak" not in [o["choice"] for o in options]


def test_connecting_flights_are_not_offered_again_after_a_re_plan_searched_them():
    assert [o["choice"] for o in plain_options(28_000, 40_000, replan_attempts=0)] == [
        "cheaper_flights",
        "increase_budget",
    ]
    assert [o["choice"] for o in plain_options(28_000, 40_000, replan_attempts=2)] == ["increase_budget"]


# ── The budget check ───────────────────────────────────────────────────────

_STATE = {
    "destination": "Goa",
    "start_date": "2026-12-10",
    "end_date": "2026-12-14",
    "budget": 40_000.0,
    "group_size": 1,
    "flights": [{"price_inr": 28_000.0}],
    "replan_attempts": 0,
    "publish_fn": None,
    "db": None,
    "trip_id": None,
}


@pytest.mark.asyncio
async def test_the_goa_conflict_is_priced_without_a_single_search():
    """Roadmap acceptance: the alternatives come from arithmetic on what the run has — no MCP call."""
    with (
        patch("src.ai.mcp_client.client.call_tool", AsyncMock()) as call_tool,
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as flights,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as hotels,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as activities,
    ):
        state = await budget_decision_node(_STATE)
    for searched in (call_tool, flights.return_value.run, hotels.return_value.run, activities.return_value.run):
        searched.assert_not_called()
    assert state["budget_decision"]["decision"] == "escalate"
    assert [o["choice"] for o in state["budget_conflict_options"]][:3] == ["cheaper_hotel", "reduce_days", "off_peak"]
    assert state["budget_estimate"]["total"] == 56_700


@pytest.mark.asyncio
async def test_the_conflict_event_carries_the_trip_as_asked_with_its_price():
    published = []

    async def publish(event):
        published.append(event)

    state = await budget_decision_node({**_STATE, "publish_fn": publish})
    await escalate_node(state)
    conflict = next(event for event in published if event.get("event") == "budget_conflict")
    assert conflict["estimate"]["total"] == 56_700 and conflict["estimate"]["total_max"] == 68_000
    assert all(isinstance(o["total"], int) for o in conflict["options"][:3])  # three alternatives, in rupees
    assert published[-1]["event"] == "planning_failed"


@pytest.mark.asyncio
async def test_a_trip_that_fits_at_typical_prices_is_not_stopped():
    """Two days in Goa: the flights are 70% of the budget, but one night and two days still fit."""
    assert make_budget_decision([{"price_inr": 28_000.0}], 40_000.0).decision == "escalate"  # the blunt rule
    state = await budget_decision_node({**_STATE, "end_date": "2026-12-11"})
    decision = state["budget_decision"]
    assert decision["decision"] == "continue" and state["budget_conflict_options"] is None
    assert decision["reason"] == (
        "Flights cost ₹28,000 — 70% of the budget — but at typical prices the rest still fits: "
        "a 4-star hotel and things to do bring the trip to about ₹35,700 of ₹40,000."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "words"), [("reduce_days", "the shorter trip"), ("cheaper_hotel", "the cheaper stay")]
)
async def test_a_way_out_the_traveller_picked_is_gone_ahead_with(choice, words):
    """The flights are the same as when they were asked: stopping them again would be a dead end."""
    state = await budget_decision_node({**_STATE, "budget_choice": {"choice": choice}})
    assert state["budget_decision"]["decision"] == "continue"
    assert (
        state["budget_decision"]["reason"]
        == f"Flights cost ₹28,000 — 70% of the budget — going ahead with {words} you chose."
    )


@pytest.mark.asyncio
async def test_a_picked_way_out_does_not_go_ahead_with_flights_dearer_than_the_budget():
    state = await budget_decision_node(
        {**_STATE, "flights": [{"price_inr": 41_000.0}], "budget_choice": {"choice": "cheaper_hotel"}}
    )
    assert state["budget_decision"]["decision"] == "escalate"  # new flights, new question


@pytest.mark.asyncio
async def test_without_a_fare_the_conflict_offers_the_plain_ways_out():
    """No flight within the budget at all: nothing to price the rest against."""
    state = await budget_decision_node({**_STATE, "flights": []})
    assert state["budget_decision"]["decision"] == "escalate" and state["budget_estimate"] is None
    assert [o["choice"] for o in state["budget_conflict_options"]] == ["cheaper_flights", "increase_budget"]


# ── Routes ─────────────────────────────────────────────────────────────────


def _escalated(trip, options: list[dict]) -> AgentRun:
    return AgentRun(
        trip_id=trip.id,
        agent_name="escalate",
        status="completed",
        output={"reason": "Flights cost ₹28,000.", "options": options, "estimate": {"total": 56_700}},
    )


@pytest.mark.asyncio
async def test_replan_shortens_the_trip_to_the_length_offered():
    trip = _trip(uuid.uuid4(), status="failed", days=4)  # five days
    offered = [{"choice": "reduce_days", "days": 2, "description": "Make it 2 days instead of 5"}]
    with _client(trip, [_escalated(trip, offered)]) as (client, spawn, redis):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": "reduce_days"})

    assert resp.status_code == 200 and spawn.called
    assert trip.end_date == START + timedelta(days=1)  # two days: one night
    assert '"choice": "reduce_days"' in redis.publish.await_args.args[1]


@pytest.mark.asyncio
async def test_replan_moves_the_trip_to_the_off_season_dates_offered():
    trip = _trip(uuid.uuid4(), status="failed", days=4)
    moved = START + timedelta(days=200)
    offered = [{"choice": "off_peak", "start_date": str(moved), "end_date": str(moved + timedelta(days=4))}]
    with _client(trip, [_escalated(trip, offered)]) as (client, _, __):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": "off_peak"})

    assert resp.status_code == 200
    assert (trip.start_date, trip.end_date) == (moved, moved + timedelta(days=4))


@pytest.mark.asyncio
async def test_replan_raises_the_budget_to_the_amount_offered():
    trip = _trip(uuid.uuid4(), status="failed")
    offered = [{"choice": "increase_budget", "budget": 56_000}]
    with _client(trip, [_escalated(trip, offered)]) as (client, _, __):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": "increase_budget"})
    assert resp.status_code == 200 and trip.budget == 56_000


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["cheaper_hotel", "off_peak", "reduce_days"])
async def test_replan_refuses_a_way_out_the_conflict_did_not_offer(choice):
    trip = _trip(uuid.uuid4(), status="failed")
    with _client(trip, [_escalated(trip, [{"choice": "increase_budget", "budget": 56_000}])]) as (client, spawn, _):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": choice})

    assert resp.status_code == 409
    assert "not offered" in resp.json()["detail"]
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_replan_refuses_off_season_dates_that_have_passed():
    trip = _trip(uuid.uuid4(), status="failed")
    gone = date.today() - timedelta(days=1)
    offered = [{"choice": "off_peak", "start_date": str(gone), "end_date": str(gone + timedelta(days=3))}]
    with _client(trip, [_escalated(trip, offered)]) as (client, spawn, _):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": "off_peak"})

    assert resp.status_code == 409 and "passed" in resp.json()["detail"]
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_a_new_way_out_needs_a_conflict_that_offered_it():
    """No conflict on record (or one from before Phase 21): a cheaper stay has nothing to go by."""
    trip = _trip(uuid.uuid4(), status="failed")
    with _client(trip) as (client, spawn, _):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": "cheaper_hotel"})
    assert resp.status_code == 409
    spawn.assert_not_called()
