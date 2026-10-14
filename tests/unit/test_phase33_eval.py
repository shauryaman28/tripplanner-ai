"""Phase 33 — the eval suite: scenarios, the driver, the measurements, the grader, the report, the command.

The records in tests/fixtures/evals/ are real: the Jaipur plan and its hotel change from the dev
database, and four that the suite itself recorded against this checkout on real providers (two budget
conflicts, the tiny budget, the past date). Breaking a copy of one is how each measurement is shown to
see what it is for. The grader is a fake that answers in the form Claude is made to answer in; the
real one is asked in scripts/run_eval.py's runs (docs/phase33_build_log.md).
"""

from __future__ import annotations

import copy
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from importlib import util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from evals import driver, grader, measure, report
from evals.scenarios import (
    BUDGET,
    BY_ID,
    COMPLETENESS,
    CRITERIA,
    DAYS_OUT,
    HALLUCINATION,
    REFINEMENT,
    SCENARIOS,
    TOOLS,
    pick,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "evals"


def script(name: str):
    spec = util.spec_from_file_location(f"scripts_{name}", ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[f"scripts_{name}"] = module
    spec.loader.exec_module(module)
    return module


run_eval = script("run_eval")


def record(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def measured(name: str, evidence: dict | None = None) -> dict[str, measure.Measure]:
    return measure.measure(BY_ID[name], evidence or record(name))


# ── The scenarios ───────────────────────────────────────────────────────────


def test_fifteen_scenarios_in_the_roadmaps_mix():
    kinds = [s.kind for s in SCENARIOS]
    assert len(SCENARIOS) == 15 and len(BY_ID) == 15
    assert {k: kinds.count(k) for k in set(kinds)} == {
        "happy path": 3,
        "budget conflict": 2,
        "ambiguous query": 2,
        "group trip": 2,
        "edge case": 4,
        "refinement": 2,
    }
    edges = {s.id for s in SCENARIOS if s.kind == "edge case"}
    assert edges == {"edge-tiny-budget", "edge-unusual-place", "edge-largest-group", "edge-past-date"}
    assert BY_ID["edge-largest-group"].group_size == 9  # the most the API takes (TripCreate: le=9)


def test_dates_are_counted_from_the_day_it_runs():
    today = date(2026, 10, 14)
    jaipur = BY_ID["happy-jaipur"].trip(today)
    assert jaipur["start_date"] == str(today + timedelta(days=DAYS_OUT)) and jaipur["end_date"] == "2026-12-01"
    assert BY_ID["edge-past-date"].trip(today)["start_date"] == "2026-10-13"  # yesterday, on purpose
    assert BY_ID["happy-goa"].trip(today)["start_date"] != BY_ID["group-goa-friends"].trip(today)["start_date"]
    group = BY_ID["group-goa-friends"].trip(today)
    assert "group_size" not in group and [m["name"] for m in group["group_members"]] == [
        "Aman",
        "Bela",
        "Chirag",
        "Dia",
    ]


def test_what_each_scenario_is_graded_on():
    assert BY_ID["happy-jaipur"].criteria == (TOOLS, BUDGET, HALLUCINATION, COMPLETENESS)
    assert BY_ID["refine-nicer-hotel"].criteria[-1] == REFINEMENT
    assert BY_ID["edge-tiny-budget"].criteria == (TOOLS, BUDGET)  # no plan: the search, and the ways out
    assert BY_ID["edge-past-date"].criteria == ()  # the refusal alone


def test_a_refinement_brings_the_plan_it_changes_and_runs_after_it():
    assert [s.id for s in pick(["refine-more-food"])] == ["happy-varanasi", "refine-more-food"]
    assert [s.id for s in pick(None)] == [s.id for s in SCENARIOS]
    with pytest.raises(ValueError, match="no such scenario"):
        pick(["happy-paris"])


# ── The driver, against a fake of the API ───────────────────────────────────


class FakePlanner:
    """Just enough of the API for the driver: accounts, trips, and runs that end at once, as they are told to."""

    def __init__(self, *endings: str) -> None:
        self.endings = list(endings)
        self.trips: dict[str, dict] = {}
        self.rows: dict[str, list[dict]] = {}
        self.plans: dict[str, list[dict]] = {}
        self.seen: list[str] = []
        self.offered = ["cheaper_flights", "increase_budget"]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.answer), base_url="http://planner")

    def answer(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.seen.append(f"{method} {path}")
        body = (
            json.loads(request.content)
            if request.content and request.headers.get("content-type") == "application/json"
            else {}
        )
        if path == "/auth/register":
            return httpx.Response(201, json={"id": "u"})
        if path == "/auth/login":
            return httpx.Response(200, json={"access_token": "a-token", "token_type": "bearer"})
        if path == "/trips" and method == "POST":
            if date.fromisoformat(body["start_date"]) < date.today():
                return httpx.Response(422, json={"detail": [{"msg": "Value error, start_date cannot be in the past"}]})
            trip_id = f"t{len(self.trips) + 1}"
            self.trips[trip_id] = {"id": trip_id, **body, "group_size": body.get("group_size", 1), "status": "pending"}
            self.rows[trip_id], self.plans[trip_id] = [], []
            return httpx.Response(201, json=self.trips[trip_id])
        trip_id, rest = re.match(r"/trips/([^/]+)(/.*)?$", path).groups()
        trip = self.trips[trip_id]
        if rest is None:
            return httpx.Response(200, json=trip)
        if rest == "/plan":
            if not trip.get("interests") and not body.get("raw_input"):
                return httpx.Response(200, json={"status": "clarification_needed", "question": "What do you enjoy?"})
            self._run(trip_id)
            return httpx.Response(202, json={"status": "planning_started"})
        if rest in ("/clarify", "/replan"):
            self._run(trip_id)
            return httpx.Response(200, json={"status": "started"})
        if rest == "/refine":
            turn = max(row["turn"] for row in self.rows[trip_id]) + 1
            self._run(trip_id, turn, refinement=True)
            return httpx.Response(
                200, json={"status": "refinement_started", "turn": turn, "refinement_type": "targeted_hotel"}
            )
        if rest == "/status":
            return httpx.Response(200, json=trip["_status"])
        if rest == "/runs":
            return httpx.Response(200, json=self.rows[trip_id])
        if rest == "/itineraries":
            return httpx.Response(200, json=list(reversed(self.plans[trip_id])))
        return httpx.Response(404)

    def _run(self, trip_id: str, turn: int = 1, refinement: bool = False) -> None:
        ending = self.endings.pop(0)
        trip, rows = self.trips[trip_id], self.rows[trip_id]

        def row(name: str, status: str = "completed", output: dict | None = None) -> None:
            rows.append({"agent_name": name, "status": status, "turn": turn, "input": {}, "output": output or {}})

        if refinement:
            row("refinement_classifier", output={"refinement_type": "targeted_hotel"})
        row("flight_agent")
        status = {"status": "failed", "budget_conflict": None, "failure_reason": None}
        if ending == "plan":
            row("itinerary_builder")
            self.plans[trip_id].append(
                {"id": f"i{len(self.plans[trip_id])}", "structured_data": {"days": []}, "total_cost": 1.0}
            )
            status["status"] = "completed"
        elif ending == "conflict":
            row("escalate")
            status["budget_conflict"] = {
                "reason": "No flights within the budget",
                "options": [{"choice": c} for c in self.offered],
            }
        elif ending == "quota":
            row("itinerary_builder", "failed")
            row(
                "builder_failed",
                output={"builder_error": {"error": "LLM call failed: Error code: 429 - rate_limit_exceeded"}},
            )
            status["failure_reason"] = "The planner could not finish the plan."
        else:
            status["failure_reason"] = "Something went wrong while this trip was being planned."
        row("orchestrator")
        trip["status"], trip["_status"] = status["status"], status


def drive(planner: FakePlanner, scenario_id: str, base: dict | None = None) -> dict:
    return driver.run(driver.Api("http://planner", planner.client()), BY_ID[scenario_id], driver.Pacer(0), base)


def test_a_plan_is_asked_for_as_a_traveller_would_and_everything_served_is_kept():
    planner = FakePlanner("plan")
    evidence = drive(planner, "happy-jaipur")
    assert evidence["outcome"] == "plan" and evidence["events"] == ["created", "planned"] and evidence["built"]
    assert planner.seen[:3] == ["POST /auth/register", "POST /auth/login", "POST /trips"]
    assert "POST /trips/t1/plan" in planner.seen
    assert len(evidence["itineraries"]) == 1 and evidence["runs"][-1]["agent_name"] == "orchestrator"
    assert evidence["asked"]["message"] == BY_ID["happy-jaipur"].message
    assert [leg["request"] for leg in evidence["legs"]] == ["POST /trips", "POST /trips/t1/plan"]


def test_the_sign_in_token_is_never_written_to_a_file(tmp_path):
    evidence = drive(FakePlanner("plan"), "happy-jaipur")
    assert evidence["_headers"] == {"Authorization": "Bearer a-token"}  # kept in memory, for a refinement
    saved = run_eval.save(evidence, tmp_path).read_text()
    assert "a-token" not in saved and "_headers" not in saved


def test_a_question_is_answered_and_the_answer_planned():
    planner = FakePlanner("plan")
    evidence = drive(planner, "ambiguous-amritsar")
    assert evidence["question"] == "What do you enjoy?"
    assert evidence["events"] == ["created", "asked", "answered", "planned"] and evidence["outcome"] == "plan"
    assert "POST /trips/t1/clarify" in planner.seen
    assert measure.outcome(BY_ID["ambiguous-amritsar"], evidence)[0] is True


def test_a_budget_conflict_takes_the_way_out_the_scenario_prefers_among_those_offered():
    planner = FakePlanner("conflict", "plan")
    planner.offered = ["cheaper_flights", "cheaper_hotel", "increase_budget"]
    evidence = drive(planner, "conflict-udaipur")  # would take reduce_days, then cheaper_hotel
    assert evidence["choice_taken"] == "cheaper_hotel" and evidence["outcome"] == "plan"
    assert evidence["events"] == ["created", "conflict", "chose cheaper_hotel", "planned"]


def test_a_conflict_with_none_of_the_wanted_ways_out_ends_there_and_says_so():
    planner = FakePlanner("conflict")
    planner.offered = ["off_peak"]
    evidence = drive(planner, "conflict-kochi")
    assert evidence["outcome"] == "conflict" and evidence["choice_taken"] is None
    assert evidence["error"] == "none of the ways out it would take was offered"


def test_a_refused_trip_is_recorded_in_words():
    evidence = drive(FakePlanner(), "edge-past-date")
    assert evidence["outcome"] == "refused" and evidence["failure_reason"] == "start_date cannot be in the past"
    assert driver.refusal("Not found") == "Not found"


def test_a_refinement_changes_the_trip_its_plan_made():
    planner = FakePlanner("plan", "plan")
    base = drive(planner, "happy-jaipur")
    evidence = drive(planner, "refine-nicer-hotel", base)
    assert evidence["graded_turn"] == 2 and evidence["outcome"] == "plan" and len(evidence["itineraries"]) == 2
    assert evidence["trip_id"] == base["trip_id"] and "POST /trips/t1/refine" in planner.seen


def test_a_refinement_whose_plan_was_not_made_is_not_run():
    evidence = drive(FakePlanner(), "refine-nicer-hotel", {"outcome": "failed"})
    assert evidence["outcome"] == "not run" and measure.outcome(BY_ID["refine-nicer-hotel"], evidence)[0] is None


def test_a_provider_refusal_for_quota_is_not_the_builds_fault():
    evidence = drive(FakePlanner("quota"), "happy-jaipur")
    assert evidence["outcome"] == "failed" and evidence["provider_refused"]
    right, said = measure.outcome(BY_ID["happy-jaipur"], evidence)
    assert right is None and "quota" in said


def test_an_ordinary_failure_is_the_builds():
    evidence = drive(FakePlanner("failed"), "happy-jaipur")
    assert not evidence["provider_refused"]
    assert measure.outcome(BY_ID["happy-jaipur"], evidence) == (
        False,
        "the trip failed: Something went wrong while this trip was being planned.",
    )


def test_the_harness_failing_is_not_run_rather_than_scored():
    broken = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)), base_url="http://x")
    evidence = driver.run(driver.Api("http://x", broken), BY_ID["happy-jaipur"], driver.Pacer(0))
    assert evidence["outcome"] == "error" and "could not make an account" in evidence["error"]
    assert measure.outcome(BY_ID["happy-jaipur"], evidence)[0] is None


def test_runs_are_told_apart_by_the_row_each_one_ends_with():
    rows = [
        {"agent_name": n}
        for n in ("flight_agent", "escalate", "orchestrator", "flight_agent", "itinerary_builder", "orchestrator", "x")
    ]
    assert [len(run) for run in driver.split_runs(rows)] == [3, 3, 1]
    assert driver.built(driver.split_runs(rows)[1]) and not driver.built(driver.split_runs(rows)[0])


def test_plans_are_kept_apart_for_the_providers_quota():
    now = [100.0]
    slept: list[float] = []
    pacer = driver.Pacer(45, sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
    assert pacer.before_a_run() == 0  # nothing built yet
    pacer.ran(began=100.0, built=True)
    now[0] = 110.0
    assert pacer.before_a_run() == 35 and slept == [35]
    pacer.ran(began=now[0], built=False)  # a run that stopped before the builder costs the quota nothing
    assert pacer.before_a_run() == 0
    assert driver.Pacer(0).before_a_run() == 0 and pacer.waited_s == 35


def test_sleep_is_told_from_the_two_clocks():
    wall, steady = [0.0], [0.0]
    awake = driver.Awake(wall=lambda: wall[0], steady=lambda: steady[0])
    wall[0], steady[0] = 130.0, 30.0
    assert awake.asleep_for() == 100.0


def test_a_wrong_ending_slept_through_is_not_scored_and_a_right_one_is():
    evidence = drive(FakePlanner("failed"), "happy-jaipur")
    evidence["asleep_s"] = 900.0
    right, said = measure.outcome(BY_ID["happy-jaipur"], evidence)
    assert right is None and "slept 900 s" in said
    good = drive(FakePlanner("plan"), "happy-jaipur") | {"asleep_s": 900.0}
    assert measure.outcome(BY_ID["happy-jaipur"], good)[0] is True


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("redis://localhost:6379", "redis://localhost:6379/7"),
        ("redis://localhost:6379/0", "redis://localhost:6379/7"),
        ("rediss://:pw@cache:6380/0?ssl_cert_reqs=none", "rediss://:pw@cache:6380/7?ssl_cert_reqs=none"),
    ],
)
def test_the_eval_has_a_redis_database_of_its_own(monkeypatch, given, expected):
    from app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", given)
    assert driver.eval_redis_url() == expected


# ── The outcome ─────────────────────────────────────────────────────────────


def test_the_real_records_end_as_they_should_except_the_dead_end_conflict():
    assert measure.outcome(BY_ID["happy-jaipur"], record("happy-jaipur"))[0] is True
    assert measure.outcome(BY_ID["edge-tiny-budget"], record("edge-tiny-budget"))[0] is True
    assert measure.outcome(BY_ID["edge-past-date"], record("edge-past-date"))[0] is True
    assert measure.outcome(BY_ID["conflict-udaipur"], record("conflict-udaipur"))[0] is True
    # ₹12,000 for two to Kochi: "raise it to ₹15,000" was taken — and the planner stopped on the budget again
    right, said = measure.outcome(BY_ID["conflict-kochi"], record("conflict-kochi"))
    assert right is False and "increase_budget was taken; then it stopped on the budget" in said


def test_a_conflict_scenario_takes_a_plan_inside_the_budget_and_not_one_over_it():
    evidence = record("happy-jaipur")  # a plan at ₹28,875, no conflict
    scenario = BY_ID["conflict-kochi"]
    assert measure.outcome(scenario, evidence) == (
        True,
        "a plan was saved with no conflict, at ₹28,875 of ₹70,000 (the conflict was not reached)",
    )
    over = copy.deepcopy(evidence)
    over["trip"]["budget"] = 20_000
    assert measure.outcome(scenario, over)[0] is False


def test_a_plan_or_a_reason_and_nothing_else_for_the_unusual_place():
    scenario = BY_ID["edge-unusual-place"]
    failed = {"outcome": "failed", "failure_reason": "Nothing was found for Ziro."}
    assert measure.outcome(scenario, failed)[0] is True
    assert measure.outcome(scenario, {"outcome": "failed", "failure_reason": None})[0] is False
    assert measure.measure(scenario, failed) == {}  # a refusal in words is graded on that alone


# ── The measurements ────────────────────────────────────────────────────────


def test_a_healthy_plan_measures_clean():
    for name in ("happy-jaipur", "conflict-udaipur", "refine-nicer-hotel"):
        assert {c: m.ceiling for c, m in measured(name).items()} == {c: 5 for c in BY_ID[name].criteria}, name


def test_the_budget_is_measured_with_the_estimate_budget_tool_at_the_prices_found():
    budget = measured("happy-jaipur")[BUDGET]
    assert "comes to ₹28,875" in budget.facts[0] and "+0.0% from the estimate" in budget.facts[1]
    evidence = record("happy-jaipur")
    evidence["itineraries"][0]["total_cost"] = 28_875 * 1.12
    problems = measured("happy-jaipur", evidence)[BUDGET].problems
    assert (2, "total_cost ₹32,340 is +12.0% off estimate_budget's ₹28,875") in problems
    assert (
        3,
        "the travellers' shares add up to ₹28,875, not the total ₹32,340",
    ) in problems  # and so they no longer add up
    evidence["itineraries"][0]["total_cost"] = 28_875 * 1.04  # within the roadmap's 5%
    assert not any(
        "off estimate_budget" in problem for _, problem in measured("happy-jaipur", evidence)[BUDGET].problems
    )


def test_the_estimate_uses_the_searchs_hotel_price_not_the_one_the_plan_copied():
    evidence = record("happy-jaipur")
    for day in evidence["itineraries"][0]["structured_data"]["days"]:
        if day.get("hotel"):
            day["hotel"]["cost_per_night"] = 1_000.0
    estimate = measure.estimate_of(measure.plan(evidence), evidence, evidence["trip"])
    assert round(estimate["nightly"], 2) == 6851.32
    assert measured("happy-jaipur", evidence)[HALLUCINATION].ceiling == 3  # and the copied price is a wrong price


def test_a_plan_over_the_travellers_budget_is_a_budget_fault():
    evidence = record("happy-jaipur")
    evidence["trip"]["budget"] = 20_000
    assert (3, "the plan costs ₹28,875, over the budget of ₹20,000") in measured("happy-jaipur", evidence)[
        BUDGET
    ].problems


def test_shares_that_do_not_add_up_are_a_budget_fault():
    evidence = record("happy-jaipur")
    evidence["itineraries"][0]["structured_data"]["per_person_breakdown"]["shares"][0]["amount"] += 500
    assert measured("happy-jaipur", evidence)[BUDGET].ceiling == 3


def test_an_invented_place_hotel_or_flight_is_found_by_name():
    evidence = record("happy-jaipur")
    days = evidence["itineraries"][0]["structured_data"]["days"]
    days[1]["morning"]["activity"] = "Sheesh Mahal Light Show"
    assert measured("happy-jaipur", evidence)[HALLUCINATION].problems == [
        (1, "places not in the attractions found: ['Sheesh Mahal Light Show']")
    ]
    evidence = record("happy-jaipur")
    evidence["itineraries"][0]["structured_data"]["days"][0]["hotel"]["name"] = "The Rambagh Palace"
    assert measured("happy-jaipur", evidence)[HALLUCINATION].ceiling == 1
    evidence = record("happy-jaipur")
    evidence["itineraries"][0]["structured_data"]["days"][0]["flight"]["flight_number"] = "AI-491"
    assert measured("happy-jaipur", evidence)[HALLUCINATION].problems == [
        (1, "flight AA AI-491 is not among the flights found")
    ]


def test_free_time_and_local_tips_are_not_hallucinations():
    evidence = record("happy-jaipur")
    evidence["itineraries"][0]["structured_data"]["days"][3]["evening"] = {"activity": "Explore the area", "cost": 0}
    m = measured("happy-jaipur", evidence)[HALLUCINATION]
    assert m.ceiling == 5 and any("not judged as hallucination" in fact for fact in m.facts)


def test_a_search_asked_with_the_wrong_parameters_is_found():
    evidence = record("happy-jaipur")
    flight = next(r for r in evidence["runs"] if r["agent_name"] == "flight_agent")
    flight["input"]["passengers"] = 1
    assert measured("happy-jaipur", evidence)[TOOLS].problems == [
        (2, "the flight search asked for passengers 1; the trip's is 2")
    ]
    evidence = record("happy-jaipur")
    activities = next(r for r in evidence["runs"] if r["agent_name"] == "activities_agent")
    activities["input"]["interests"] = ["sightseeing"]
    assert measured("happy-jaipur", evidence)[TOOLS].problems == [
        (2, "the attractions search left out what the traveller enjoys: ['food', 'history']")
    ]
    evidence = record("happy-jaipur")
    hotel = next(r for r in evidence["runs"] if r["agent_name"] == "hotel_agent")
    hotel["input"]["check_out"] = "2026-11-30"
    assert measured("happy-jaipur", evidence)[TOOLS].ceiling == 2


def test_a_search_never_made_is_found():
    evidence = record("happy-jaipur")
    evidence["runs"] = [r for r in evidence["runs"] if r["agent_name"] != "hotel_agent"]
    assert (2, "the hotel search was never made") in measured("happy-jaipur", evidence)[TOOLS].problems


def test_a_change_that_searches_more_than_it_needs_is_found():
    evidence = record("refine-nicer-hotel")
    extra = copy.deepcopy(next(r for r in evidence["runs"] if r["agent_name"] == "activities_agent"))
    extra["turn"] = 2
    at = next(i for i, r in enumerate(evidence["runs"]) if r["turn"] == 2 and r["agent_name"] == "hotel_agent")
    evidence["runs"].insert(at, extra)
    assert measured("refine-nicer-hotel", evidence)[TOOLS].problems == [
        (3, "a change to the hotel searched for attractions again too")
    ]


def test_a_missing_day_and_an_empty_slot_with_places_left_are_found():
    evidence = record("happy-jaipur")
    days = evidence["itineraries"][0]["structured_data"]["days"]
    del days[2]
    assert (1, "no plan for ['2026-11-27']") in measured("happy-jaipur", evidence)[COMPLETENESS].problems
    evidence = record("happy-jaipur")
    evidence["itineraries"][0]["structured_data"]["days"][0]["evening"] = None  # Jaipur Zoo, now unused
    assert measured("happy-jaipur", evidence)[COMPLETENESS].problems == [
        (3, "3 slots left empty while 1 places found were not used")
    ]


def test_an_empty_slot_is_complete_when_every_place_found_is_used():
    days = measure.plan(record("happy-jaipur"))["days"]
    assert any(slot is None for day in days for _, slot in measure.slots(day))  # the real plan has empty evenings
    assert measured("happy-jaipur")[COMPLETENESS].ceiling == 5  # …and all ten places found are in it


def test_a_traveller_with_nothing_for_them_is_found():
    evidence = record("happy-jaipur")
    evidence["trip"]["group_members"] = [
        {"name": "Asha", "interests": ["food"]},
        {"name": "Ravi", "interests": ["history"]},
    ]
    for day in evidence["itineraries"][0]["structured_data"]["days"]:
        for name in ("morning", "afternoon", "evening"):
            if day.get(name):
                day[name]["suits"] = ["Asha"]
    assert measured("happy-jaipur", evidence)[COMPLETENESS].problems == [
        (3, "nothing in the plan suits Ravi, who enjoys ['history']")
    ]


def test_a_change_that_did_not_change_what_was_asked_or_changed_more_is_found():
    evidence = record("refine-nicer-hotel")
    after, before = (i["structured_data"] for i in evidence["itineraries"][:2])
    m = measured("refine-nicer-hotel", evidence)[REFINEMENT]
    assert m.ceiling == 5 and "Changed: dates no, flight no, hotel yes, activities no" in m.facts

    unchanged = copy.deepcopy(evidence)
    unchanged["itineraries"][0]["structured_data"] = copy.deepcopy(before)
    assert (1, "the hotel is as it was: the change asked for was not made") in measured(
        "refine-nicer-hotel", unchanged
    )[REFINEMENT].problems

    reshuffled = copy.deepcopy(evidence)
    days = reshuffled["itineraries"][0]["structured_data"]["days"]
    days[0]["morning"], days[1]["morning"] = days[1]["morning"], days[0]["morning"]
    days[0]["afternoon"], days[1]["afternoon"] = days[1]["afternoon"], days[0]["afternoon"]
    assert measured("refine-nicer-hotel", reshuffled)[REFINEMENT].problems == [
        (2, "4 activity slots changed, though only the hotel was asked about")
    ]

    other_flight = copy.deepcopy(evidence)
    other_flight["itineraries"][0]["structured_data"]["days"][0]["flight"]["flight_number"] = "AA-99"
    assert (2, "the flight changed, though only the hotel was asked about") in measured(
        "refine-nicer-hotel", other_flight
    )[REFINEMENT].problems
    assert after["days"][0]["flight"]["flight_number"] == "AA-10"  # the record itself is untouched


def test_a_change_read_as_the_wrong_kind_is_found():
    evidence = record("refine-nicer-hotel")
    classifier = next(r for r in evidence["runs"] if r["agent_name"] == "refinement_classifier")
    classifier["output"]["refinement_type"] = "full_replan"
    assert (2, "the change was read as 'full_replan', not 'targeted_hotel'") in measured(
        "refine-nicer-hotel", evidence
    )[REFINEMENT].problems


def test_a_conflict_is_measured_on_its_search_and_the_ways_out_it_offered():
    m = measured("edge-tiny-budget")
    assert set(m) == {TOOLS, BUDGET} and m[TOOLS].ceiling == 5
    assert "Offered: increase_budget — Increase total budget to ₹3,750" in m[BUDGET].facts
    evidence = record("edge-tiny-budget")
    evidence["conflict"]["options"] = []
    assert measured("edge-tiny-budget", evidence)[BUDGET].problems == [
        (1, "it stopped on the budget and offered no way out")
    ]


def test_a_raise_that_does_not_pay_for_the_cheapest_flight_found_is_found():
    evidence = record("edge-tiny-budget")
    flight = next(r for r in evidence["runs"] if r["agent_name"] == "flight_agent")
    flight["output"]["flights"] = [{"airline": "ZZ", "flight_number": "ZZ-1", "price_inr": 9_800.0}]
    assert measured("edge-tiny-budget", evidence)[BUDGET].problems == [
        (2, "the budget it offered, ₹3,750, does not pay for the cheapest flight found, ₹9,800")
    ]


# ── The grader ──────────────────────────────────────────────────────────────


class FakeClaude:
    """Answers as Claude is made to — a call of record_grades — with the scores it is given."""

    def __init__(self, *scores: int | dict, broken_first: bool = False) -> None:
        self.scores, self.broken_first, self.sent = list(scores), broken_first, []
        self.messages = self

    def create(self, **kwargs):
        self.sent.append(kwargs)
        asked = kwargs["tools"][0]["input_schema"]["properties"]["grades"]["items"]["properties"]["criterion"]["enum"]
        if self.broken_first and len(self.sent) == 1:
            answer = {"grades": [{"criterion": asked[0], "score": 9, "reasons": []}], "summary": ""}
        else:
            score = self.scores[0] if len(self.scores) == 1 else self.scores.pop(0)
            given = score if isinstance(score, dict) else {c: score for c in asked}
            answer = {
                "grades": [
                    {"criterion": c, "score": given[c], "reasons": [] if given[c] == 5 else [f"{c} is off"]}
                    for c in asked
                ],
                "summary": "A fair plan.",
            }
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="…"), SimpleNamespace(type="tool_use", input=answer)],
            usage=SimpleNamespace(input_tokens=4000, output_tokens=300),
        )


def grade(name: str, client: FakeClaude, evidence: dict | None = None) -> grader.Graded:
    evidence = evidence or record(name)
    scenario = BY_ID[name]
    _, said = measure.outcome(scenario, evidence)
    return grader.grade(client, scenario, evidence, said, measure.measure(scenario, evidence))


def test_the_grader_is_haiku_made_to_answer_through_a_tool():
    client = FakeClaude(4)
    graded = grade("happy-jaipur", client)
    sent = client.sent[0]
    assert sent["model"] == "claude-haiku-4-5-20251001" == grader.MODEL
    assert sent["tool_choice"] == {"type": "tool", "name": "record_grades"}
    assert sent["tools"][0]["input_schema"]["properties"]["grades"]["items"]["properties"]["score"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 5,
    }
    assert graded.scores == {TOOLS: 4, BUDGET: 4, HALLUCINATION: 4, COMPLETENESS: 4}
    assert graded.reasons[TOOLS] == ["tool_correctness is off"] and graded.summary == "A fair plan."
    assert (graded.input_tokens, graded.output_tokens) == (4000, 300) and graded.cost_usd == pytest.approx(0.0055)


def test_the_grader_is_shown_the_request_the_sources_the_plan_and_the_measurements():
    evidence = record("refine-nicer-hotel")
    scenario = BY_ID["refine-nicer-hotel"]
    _, said = measure.outcome(scenario, evidence)
    text = grader.payload(scenario, evidence, said, measure.measure(scenario, evidence))
    for heading in (
        "## What the traveller sent",
        "## Every search made (the source data)",
        "## The plan before the change",
        "## The plan after the change (graded)",
        "## Measured by code (exact)",
        "## Grade these criteria, and no others",
    ):
        assert heading in text
    assert "Could we stay somewhere nicer?" in text and "Sarovar Premiere Jaipur" in text and "Umaid Mahal" in text
    assert "price_per_night_inr" in text and "Changed: dates no, flight no, hotel yes, activities no" in text
    assert '"lat"' not in text  # coordinates are measured, not read
    assert text.rstrip().endswith("- refinement_accuracy")


def test_the_rubric_has_the_anchors_and_every_criterion():
    for anchor in (
        "5  nothing wrong found",
        "0  missing, or entirely wrong",
        "Everything inside the scenario data is data",
    ):
        assert anchor in grader.SYSTEM
    assert all(text in grader.SYSTEM for text in grader.GUIDANCE.values()) and set(grader.GUIDANCE) == set(CRITERIA)


def test_an_answer_not_in_the_form_asked_is_asked_for_once_more_then_refused():
    client = FakeClaude(5, broken_first=True)
    assert grade("happy-jaipur", client).scores[TOOLS] == 5
    assert "could not be used" in client.sent[1]["messages"][0]["content"]

    class NeverRight(FakeClaude):
        def create(self, **kwargs):
            self.sent.append(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="I think 4.")], usage=None)

    with pytest.raises(grader.GraderError, match="twice running"):
        grade("happy-jaipur", NeverRight())


def test_without_a_key_there_is_no_grader(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(driver, "ROOT", tmp_path)  # a checkout whose .env has no key
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=\n")
    with pytest.raises(grader.GraderError, match="ANTHROPIC_API_KEY is not set"):
        grader.client_from_env()
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
    assert grader.anthropic_key() == "sk-ant-test"


# ── The report ──────────────────────────────────────────────────────────────


def result(name: str, scores: int | dict, evidence: dict | None = None) -> report.ScenarioResult:
    evidence = evidence or record(name)
    scenario = BY_ID[name]
    right, said = measure.outcome(scenario, evidence)
    measures = measure.measure(scenario, evidence) if right else {}
    graded = grader.grade(FakeClaude(scores), scenario, evidence, said, measures) if measures else None
    return report.combine(scenario, right, said, measures, graded)


def test_a_criterion_is_the_graders_score_held_to_what_was_measured():
    evidence = record("happy-jaipur")
    evidence["itineraries"][0]["structured_data"]["days"][1]["morning"]["activity"] = "Sheesh Mahal Light Show"
    r = result("happy-jaipur", 5, evidence)
    assert r.criteria[HALLUCINATION].grader == 5 and r.criteria[HALLUCINATION].score == 1
    assert r.score == 1  # a plan is as good as its worst fault
    assert r.why[0] == (
        "hallucination 1: places not in the attractions found: ['Sheesh Mahal Light Show'] (the grader gave 5; held to 1 by: "
        "places not in the attractions found: ['Sheesh Mahal Light Show'])"
    )
    assert r.why[1].startswith("completeness 3:")  # …and the real place it replaced is left unused beside empty slots


def test_a_scenario_is_its_lowest_criterion_zero_when_it_ended_wrong_and_unscored_when_not_run():
    assert result("happy-jaipur", {TOOLS: 5, BUDGET: 4, HALLUCINATION: 5, COMPLETENESS: 3}).score == 3
    assert result("conflict-kochi", 5).score == 0
    assert result("edge-past-date", 5).score == 5  # refused as it should be: nothing else to grade
    not_run = report.combine(BY_ID["happy-goa"], None, "not run: no record", {}, None)
    assert not_run.score is None and not_run.why == ["not run: no record"]


def test_one_criterion_failing_everywhere_fails_the_suite():
    # Why the lowest and not the average: four criteria at 5, 5, 5 and 1 average 4.0, which would pass 3.5.
    broken = {TOOLS: 1, BUDGET: 5, HALLUCINATION: 5, COMPLETENESS: 5, REFINEMENT: 5}
    suite = report.Suite([result(n, broken) for n in ("happy-jaipur", "conflict-udaipur", "refine-nicer-hotel")], 3.5)
    assert suite.average == 1.0 and suite.verdict == report.BELOW


def test_the_suite_verdict():
    healthy = [result("happy-jaipur", 5), result("conflict-udaipur", 4), result("edge-past-date", 5)]
    suite = report.Suite(healthy, 3.5)
    assert suite.average == pytest.approx(4.67) and suite.verdict == report.PASSED
    assert suite.criterion_average(BUDGET) == 4.5 and suite.criterion_average(REFINEMENT) is None
    assert "average 4.67 ≥ 3.5 — passed" == suite.verdict_words()
    low = report.Suite([result("happy-jaipur", 2), result("conflict-kochi", 5)], 3.5)
    assert low.average == 1.0 and low.verdict == report.BELOW and "BELOW THE THRESHOLD" in low.verdict_words()
    incomplete = report.Suite([*healthy, report.combine(BY_ID["happy-goa"], None, "not run: x", {}, None)], 3.5)
    assert incomplete.verdict == report.INCOMPLETE and incomplete.average == suite.average
    assert report.Suite(healthy, None).verdict == report.PASSED


def test_the_table():
    suite = report.Suite(
        [
            result("happy-jaipur", 5),
            result("conflict-kochi", 5),
            report.combine(BY_ID["happy-goa"], None, "not run: a | b", {}, None),
        ],
        3.5,
        run_at="2026-10-14T05:00:00",
        commit="abcdef1234",
        seconds=605,
        waited_s=300,
    )
    text = report.markdown(suite)
    assert text.startswith("# Eval results — 2026-10-14\n")
    assert "| 1 | `happy-jaipur` | happy path | 5 | 5 | 5 | 5 | · | **5** | — |" in text
    assert "| 2 | `conflict-kochi` | budget conflict | · | · | · | · | · | **0** | expected conflict:" in text
    assert "not run: a \\| b" in text  # a pipe in a reason cannot break the table
    assert "Took 10 min 05 s, of which 5 min 00 s waiting" in text and "does not hold" not in text
    suite.asleep_s = 900
    assert "**this time does not hold: the machine slept 15 min 00 s of it**" in report.markdown(suite)


def test_the_history_grows_by_one_line_a_run_and_keeps_the_rest(tmp_path):
    path = tmp_path / "eval-history.csv"
    first = report.Suite([result("happy-jaipur", 5)], 3.5, run_at="2026-10-14T05:00:00", commit="a" * 40, seconds=300)
    second = report.Suite([result("happy-jaipur", 2)], 3.5, label="broken", run_at="2026-10-14T06:00:00", seconds=300)
    report.append_history(path, first)
    report.append_history(path, second)
    rows = list(csv.DictReader(path.open()))
    assert [r["average"] for r in rows] == ["5.00", "2.00"] and [r["verdict"] for r in rows] == ["passed", "below"]
    assert rows[0]["commit"] == "a" * 12 and rows[1]["label"] == "broken" and rows[0]["tool_correctness"] == "5.00"
    assert path.read_text().count("run_at,") == 1  # the header once
    second.asleep_s = 60
    report.append_history(path, second)
    assert list(csv.DictReader(path.open()))[2]["minutes"] == ""  # a time slept through is no time
    other = tmp_path / "other.csv"
    other.write_text("date,score\n2026-10-01,4\n")
    with pytest.raises(ValueError, match="not appending"):
        report.append_history(other, first)
    assert other.read_text() == "date,score\n2026-10-01,4\n"


def test_the_nightly_report():
    suite = report.Suite(
        [result("happy-jaipur", 5), report.combine(BY_ID["happy-goa"], None, "not run: quota", {}, None)], 3.5
    )
    root = ET.fromstring(report.junit(suite))
    cases = {c.get("name"): c for c in root.iter("testcase")}
    assert set(cases) == {"happy-jaipur: 5/5", "happy-goa", "average score ≥ 3.5", "every scenario run and graded"}
    assert (
        cases["happy-goa"].find("skipped") is not None
        and cases["every scenario run and graded"].find("failure") is not None
    )
    assert cases["average score ≥ 3.5"].find("failure") is None
    low = ET.fromstring(report.junit(report.Suite([result("happy-jaipur", 1)], 3.5)))
    assert low.get("failures") == "1" and "average 1.0 < 3.5" in low.find(".//failure").get("message")
    unset = ET.fromstring(report.junit(report.Suite([], 3.5), not_configured="ANTHROPIC_API_KEY is not set"))
    assert unset.get("skipped") == "1" and unset.get("failures") == "0"


# ── The command ─────────────────────────────────────────────────────────────


@pytest.fixture
def regrading(tmp_path, monkeypatch):
    """scripts/run_eval.py --regrade over the real records, with a fake Claude and its own history file."""
    monkeypatch.setattr(run_eval, "HISTORY", tmp_path / "eval-history.csv")

    def run(*args: str, claude: FakeClaude | Exception | None = None) -> int:
        def client():
            if isinstance(claude, Exception):
                raise claude
            return claude

        monkeypatch.setattr(grader, "client_from_env", client)
        return run_eval.main(["--regrade", str(FIXTURES), "--results", str(tmp_path), *args])

    return run


def test_a_healthy_build_exits_0_and_a_broken_one_exits_1(regrading, tmp_path):
    assert regrading("--assert-min-score", "3.5", claude=FakeClaude(5)) == report.PASSED
    results = list(tmp_path.glob("eval-results-*-regrade.md"))
    assert len(results) == 1 and "**average 4.17 ≥ 3.5 — passed**" in results[0].read_text()  # 5 of 6 at 5; kochi 0
    assert regrading("--assert-min-score", "3.5", "--label", "broken", claude=FakeClaude(2)) == report.BELOW
    rows = list(csv.DictReader((tmp_path / "eval-history.csv").open()))
    assert [(r["label"], r["verdict"], r["scored"]) for r in rows] == [
        ("regrade", "passed", "6"),
        ("broken", "below", "6"),
    ]


def test_no_grader_no_verdict(regrading, tmp_path):
    junit = tmp_path / "evals.xml"
    code = regrading("--junit", str(junit), claude=grader.GraderError("ANTHROPIC_API_KEY is not set"))
    assert code == report.INCOMPLETE and ET.fromstring(junit.read_text()).get("skipped") == "1"
    assert not (tmp_path / "eval-history.csv").exists()


def test_a_scenario_the_grader_fails_on_is_not_scored(regrading, tmp_path):
    class Flaky(FakeClaude):
        def create(self, **kwargs):
            if "refine-nicer-hotel" in kwargs["messages"][0]["content"]:
                raise RuntimeError("overloaded")
            return super().create(**kwargs)

    assert regrading("--assert-min-score", "3.5", claude=Flaky(5)) == report.INCOMPLETE
    text = next(tmp_path.glob("eval-results-*.md")).read_text()
    assert "not graded: RuntimeError: overloaded" in text


def test_without_the_provider_keys_the_suite_is_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(grader, "client_from_env", lambda: FakeClaude(5))
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)  # no .env
    for name in run_eval.NEEDED_FOR_PLANS:
        monkeypatch.delenv(name, raising=False)
    junit = tmp_path / "evals.xml"
    assert run_eval.main(["--junit", str(junit)]) == report.INCOMPLETE
    assert "GROQ_API_KEY, DUFFEL_ACCESS_TOKEN, LITEAPI_API_KEY, OPENTRIPMAP_API_KEY" in junit.read_text()


def test_the_eval_is_no_part_of_the_app():
    app_code = "\n".join(path.read_text() for path in (ROOT / "src").rglob("*.py"))
    assert "from evals" not in app_code and "import evals" not in app_code
    dockerfile = (ROOT / "src" / "backend" / "Dockerfile").read_text()
    assert "evals" not in dockerfile and "COPY src/ ./src/" in dockerfile
    assert ".evals/" in (ROOT / ".gitignore").read_text().splitlines()
