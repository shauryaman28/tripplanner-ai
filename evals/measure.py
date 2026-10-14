"""What can be measured exactly, before anything is read by a model (Phase 33).

For each criterion, a `Measure`: the facts found (shown to the grader, in plain sentences) and the
problems found, each with the highest score it leaves possible — its *ceiling*. A total that is 12%
off the estimate is not a matter of opinion, and the grader is not asked to redo the arithmetic: it is
told the result. What it scores, it scores no higher than the ceilings allow (report.py).

The outcome comes first: a scenario that did not end the way it should — a plan where the budget
cannot pay for one, a crash where a plan was asked for — scores 0, and nothing else is graded.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from evals.driver import split_runs
from evals.scenarios import BUDGET, COMPLETENESS, HALLUCINATION, REFINEMENT, TOOLS, Scenario
from src.ai.itinerary import FREE_TIME, SLOTS

SEARCHES = ("flight_agent", "hotel_agent", "activities_agent")
SEARCH_WORDS = {"flight_agent": "flight", "hotel_agent": "hotel", "activities_agent": "attractions"}
CHANGES_SEARCH = {"flight": "flight_agent", "hotel": "hotel_agent", "activities": "activities_agent"}
ESTIMATE_TOLERANCE = 0.05  # the roadmap's: total_cost within 5% of estimate_budget
OVER_BUDGET_TOLERANCE = 0.05
PRICE_TOLERANCE_INR, PRICE_TOLERANCE_PCT = 50.0, 0.01  # as the planner's own evaluator allows a copied price


@dataclass
class Measure:
    criterion: str
    facts: list[str] = field(default_factory=list)
    problems: list[tuple[int, str]] = field(default_factory=list)  # (the highest score it leaves possible, what)

    @property
    def ceiling(self) -> int:
        return min((ceiling for ceiling, _ in self.problems), default=5)

    def found(self, ceiling: int, what: str) -> None:
        self.problems.append((ceiling, what))

    def to_dict(self) -> dict:
        return {"criterion": self.criterion, "ceiling": self.ceiling, "facts": self.facts, "problems": self.problems}


# ── Reading the evidence ────────────────────────────────────────────────────


def graded_rows(evidence: dict) -> list[dict]:
    """The rows of the turn that is graded (a refinement's own; otherwise the first)."""
    turn = evidence.get("graded_turn") or 1
    return [row for row in evidence.get("runs") or [] if row.get("turn", 1) == turn]


def last_run(evidence: dict) -> list[dict]:
    runs = split_runs(graded_rows(evidence))
    return runs[-1] if runs else []


def latest(rows: list[dict], agent: str) -> dict | None:
    return next((row for row in reversed(rows) if row["agent_name"] == agent), None)


def plan(evidence: dict) -> dict | None:
    """The itinerary the graded run saved (GET /itineraries is newest first), with its total."""
    itineraries = evidence.get("itineraries") or []
    if not itineraries or evidence.get("outcome") != "plan":
        return None
    newest = itineraries[0]
    return {**(newest.get("structured_data") or {}), "total_cost": newest.get("total_cost")}


def previous_plan(evidence: dict) -> dict | None:
    itineraries = evidence.get("itineraries") or []
    if len(itineraries) < 2:
        return None
    return {**(itineraries[1].get("structured_data") or {}), "total_cost": itineraries[1].get("total_cost")}


def found_by(evidence: dict, agent: str, key: str) -> list[dict]:
    """Everything that search found, in every run of the trip — a refined plan keeps what earlier ones found."""
    items: list[dict] = []
    for row in evidence.get("runs") or []:
        if row["agent_name"] == agent:
            items.extend(((row.get("output") or {}).get(key)) or [])
    return items


def latest_found(evidence: dict, agent: str, key: str) -> list[dict]:
    """What the last such search found — what the plan was built from."""
    row = latest(evidence.get("runs") or [], agent)
    return list(((row or {}).get("output") or {}).get(key) or [])


def slots(day: dict):
    for name in SLOTS:
        slot = day.get(name)
        yield name, (slot if slot and slot.get("activity") else None)


def _trip_dates(trip: dict) -> list[str]:
    start, end = date.fromisoformat(trip["start_date"]), date.fromisoformat(trip["end_date"])
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def _same_price(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return True
    return abs(a - b) <= max(PRICE_TOLERANCE_INR, PRICE_TOLERANCE_PCT * abs(b))


def _inr(amount: float | None) -> str:
    return "—" if amount is None else f"₹{amount:,.0f}"


# ── The outcome ─────────────────────────────────────────────────────────────


A_NAP_S = 1.0  # less than this is the two clocks being read a moment apart


def outcome(scenario: Scenario, evidence: dict) -> tuple[bool | None, str]:
    """(right, what happened). None: the scenario could not be run — not the build's doing, or not known to be.

    A scenario that ended wrong while the machine slept under it is not counted against the build: a plan
    slept through can fail for that alone. One that ended right is counted — sleep cannot make a plan good.
    """
    right, said = _outcome(scenario, evidence)
    asleep = evidence.get("asleep_s") or 0.0
    if right is False and asleep >= A_NAP_S:
        return (
            None,
            f"not run: the machine slept {asleep:.0f} s during it, and it ended wrong ({said}) — run it again awake",
        )
    return right, said


def _outcome(scenario: Scenario, evidence: dict) -> tuple[bool | None, str]:
    got = evidence.get("outcome")
    reason = evidence.get("failure_reason")
    if got in ("error", "not run", None):
        return None, f"not run: {evidence.get('error') or 'no record'}"
    if evidence.get("provider_refused") and got != "plan":
        return None, "not run: a model provider refused for quota (HTTP 429)"

    said = {
        "plan": "a plan was saved",
        "conflict": "it stopped on the budget and offered ways out",
        "refused": f"the API refused the trip ({reason})",
        "failed": f"the trip failed: {reason or 'no reason given'}",
        "question": "it asked a question and the trip ended there",
    }[got]
    if evidence.get("question"):
        said = f"it asked “{evidence['question']}”; then {said}"
    if evidence.get("choice_taken"):
        said = f"it stopped on the budget; {evidence['choice_taken']} was taken; then {said}"

    expect = scenario.expect
    if expect == "refused":
        return got == "refused", said
    if expect == "question":
        return bool(evidence.get("question")) and got == "plan", said
    if expect == "plan":
        return got == "plan", said
    if expect == "plan or reason":
        return got in ("plan", "conflict") or (got == "failed" and bool(reason)), said
    # a budget conflict: the conflict (and, with a choice, the plan after it) — or a plan inside the budget
    if not evidence.get("conflict") and got == "plan":
        total, budget = (plan(evidence) or {}).get("total_cost"), (evidence.get("trip") or {}).get("budget")
        inside = total is not None and budget is not None and total <= budget * (1 + OVER_BUDGET_TOLERANCE)
        return inside, f"{said} with no conflict, at {_inr(total)} of {_inr(budget)}" + (
            " (the conflict was not reached)" if inside else " — over the budget"
        )
    return (got == "plan") if scenario.choice else (got == "conflict"), said


# ── Tool correctness ────────────────────────────────────────────────────────


def tools(scenario: Scenario, evidence: dict) -> Measure:
    m = Measure(TOOLS)
    trip = evidence.get("trip") or {}
    run = last_run(evidence)
    ran = [row["agent_name"] for row in run if row["agent_name"] in SEARCHES]
    m.facts.append(
        "The run being graded made these searches: " + (", ".join(SEARCH_WORDS[name] for name in ran) or "none")
    )
    if len(split_runs(graded_rows(evidence))) > 1:
        m.facts.append(
            f"It was the trip's run number {len(split_runs(graded_rows(evidence)))} (earlier ones are shown too)."
        )

    if scenario.expect == "conflict" and evidence.get("outcome") == "conflict":
        expected: tuple[str, ...] = ("flight_agent",)
    elif scenario.refines:
        expected = (CHANGES_SEARCH[scenario.changes],) if scenario.changes in CHANGES_SEARCH else ()
        for name in sorted(set(ran) - set(expected)):
            m.found(3, f"a change to the {scenario.changes} searched for {SEARCH_WORDS[name]} again too")
    else:
        expected = SEARCHES
    for name in expected:
        if name not in ran:
            m.found(2, f"the {SEARCH_WORDS[name]} search was never made")

    group_size = trip.get("group_size") or 1
    flight = latest(run, "flight_agent")
    if flight:
        sent = flight.get("input") or {}
        m.facts.append(
            f"Flight search asked for: {_params(sent, ('origin', 'destination', 'date', 'return_date', 'passengers', 'budget', 'max_stops'))}"
        )
        _expect(m, "flight search", "date", sent.get("date"), trip.get("start_date"))
        _expect(m, "flight search", "return date", sent.get("return_date"), trip.get("end_date"))
        _expect(m, "flight search", "passengers", sent.get("passengers"), group_size)
        _expect_place(m, "flight search", sent.get("destination"), trip.get("destination"))
    hotel = latest(run, "hotel_agent")
    if hotel:
        sent = hotel.get("input") or {}
        m.facts.append(
            f"Hotel search asked for: {_params(sent, ('destination', 'check_in', 'check_out', 'guests', 'budget_per_night'))}"
        )
        _expect(m, "hotel search", "check-in", sent.get("check_in"), trip.get("start_date"))
        _expect(m, "hotel search", "check-out", sent.get("check_out"), trip.get("end_date"))
        _expect(m, "hotel search", "guests", sent.get("guests"), group_size)
        _expect_place(m, "hotel search", sent.get("destination"), trip.get("destination"))
        nights = max(1, len(_trip_dates(trip)) - 1) if trip.get("start_date") else 1
        per_night = sent.get("budget_per_night")
        if (
            per_night is not None
            and trip.get("budget")
            and per_night * nights > trip["budget"] * (1 + OVER_BUDGET_TOLERANCE)
        ):
            m.found(
                3,
                f"the hotel search allowed {_inr(per_night)} a night — {_inr(per_night * nights)} for the stay, more than the whole budget of {_inr(trip['budget'])}",
            )
    activities = latest(run, "activities_agent")
    if activities:
        sent = activities.get("input") or {}
        m.facts.append(f"Attractions search asked for: {_params(sent, ('destination', 'interests', 'limit', 'days'))}")
        _expect_place(m, "attractions search", sent.get("destination"), trip.get("destination"))
        wanted = {interest.casefold() for interest in trip.get("interests") or []}
        searched = {interest.casefold() for interest in sent.get("interests") or []}
        if wanted - searched:
            m.found(2, f"the attractions search left out what the traveller enjoys: {sorted(wanted - searched)}")
        members = [member["name"] for member in trip.get("group_members") or []]
        sent_members = [member.get("name") for member in sent.get("group_members") or []]
        if members and sorted(members) != sorted(sent_members):
            m.found(2, f"the attractions search was not told who travels: {sent_members or 'nobody'}, not {members}")

    asked = (evidence.get("asked") or {}).get("trip") or {}
    choice = evidence.get("choice_taken")
    if choice == "increase_budget":
        if not trip.get("budget") or trip["budget"] <= (asked.get("budget") or 0):
            m.found(2, "the budget was to be raised, and was not")
        else:
            m.facts.append(
                f"The budget was raised from {_inr(asked.get('budget'))} to {_inr(trip['budget'])}, as chosen."
            )
    if choice == "reduce_days":
        if asked.get("end_date") and trip.get("end_date") and trip["end_date"] >= asked["end_date"]:
            m.found(2, "the trip was to be shortened, and was not")
        else:
            m.facts.append(
                f"The trip was shortened to end on {trip.get('end_date')} (asked: {asked.get('end_date')}), as chosen."
            )
    return m


def _params(sent: dict, keys: tuple[str, ...]) -> str:
    return ", ".join(f"{key}={sent.get(key)!r}" for key in keys if key in sent)


def _expect(m: Measure, search: str, what: str, sent, wanted) -> None:
    if wanted is not None and sent != wanted:
        m.found(2, f"the {search} asked for {what} {sent!r}; the trip's is {wanted!r}")


def _expect_place(m: Measure, search: str, sent: str | None, wanted: str | None) -> None:
    if sent and wanted and sent.casefold() not in wanted.casefold() and wanted.casefold() not in sent.casefold():
        m.found(2, f"the {search} looked in {sent!r}; the trip goes to {wanted!r}")


# ── Budget consistency ──────────────────────────────────────────────────────


def budget(scenario: Scenario, evidence: dict) -> Measure:
    m = Measure(BUDGET)
    trip = evidence.get("trip") or {}
    fares = [f.get("price_inr") for f in latest_found(evidence, "flight_agent", "flights") if f.get("price_inr")]
    conflict = evidence.get("conflict")
    if conflict:
        m.facts.append(f"The budget check said: {conflict.get('reason')}")
        for option in conflict.get("options") or []:
            m.facts.append(f"Offered: {option.get('choice')} — {option.get('description')}")
    the_plan = plan(evidence)
    if the_plan is None:
        if evidence.get("outcome") == "conflict":
            options = (conflict or {}).get("options") or []
            if not options:
                m.found(1, "it stopped on the budget and offered no way out")
            cheapest = min(fares, default=None)
            for option in options:
                if option.get("choice") == "increase_budget" and cheapest and (option.get("budget") or 0) < cheapest:
                    m.found(
                        2,
                        f"the budget it offered, {_inr(option.get('budget'))}, does not pay for the cheapest flight found, {_inr(cheapest)}",
                    )
            if cheapest:
                m.facts.append(
                    f"The cheapest flight found costs {_inr(cheapest)}; the budget is {_inr(trip.get('budget'))}."
                )
            if evidence.get("itineraries"):
                m.found(1, "an itinerary was saved although the budget could not pay for the trip")
        return m

    total = the_plan.get("total_cost")
    estimate = estimate_of(the_plan, evidence, trip)
    m.facts.append(
        f"The plan's total_cost is {_inr(total)}. estimate_budget, given the plan's own parts priced from the searches "
        f"(flight {_inr(estimate['flights'])}, {estimate['nights']} hotel nights at {_inr(estimate['nightly'])}, "
        f"activities {_inr(estimate['activities'])}), comes to {_inr(estimate['total'])}."
    )
    if total is None:
        m.found(1, "the plan has no total cost")
    elif estimate["total"] > 0:
        off = (total - estimate["total"]) / estimate["total"]
        m.facts.append(f"That is {off:+.1%} from the estimate (within ±{ESTIMATE_TOLERANCE:.0%} is consistent).")
        if abs(off) > ESTIMATE_TOLERANCE:
            m.found(2, f"total_cost {_inr(total)} is {off:+.1%} off estimate_budget's {_inr(estimate['total'])}")
    if total is not None and trip.get("budget"):
        m.facts.append(f"The traveller's budget is {_inr(trip['budget'])}.")
        if total > trip["budget"] * (1 + OVER_BUDGET_TOLERANCE):
            m.found(3, f"the plan costs {_inr(total)}, over the budget of {_inr(trip['budget'])}")
    split = the_plan.get("per_person_breakdown")
    if split and total is not None:
        shares = sum(share.get("amount") or 0 for share in split.get("shares") or [])
        m.facts.append(
            f"Per person: {_inr(split.get('total'))} each for {split.get('travellers')}; the shares add up to {_inr(shares)}."
        )
        if abs(shares - total) > (split.get("travellers") or 1):
            m.found(3, f"the travellers' shares add up to {_inr(shares)}, not the total {_inr(total)}")
    elif (trip.get("group_size") or 1) > 1 and the_plan.get("per_person_cost") is None:
        m.facts.append("No per-person cost is given for this group.")
    return m


def estimate_of(the_plan: dict, evidence: dict, trip: dict) -> dict:
    """estimate_budget (the tool itself) for what the plan contains, at the prices the searches found."""
    from src.ai.mcp_server.models import BudgetInput
    from src.ai.mcp_server.tools import estimate_budget

    days = the_plan.get("days") or []
    flights = found_by(evidence, "flight_agent", "flights")
    hotels = {h.get("name"): h.get("price_per_night_inr") for h in found_by(evidence, "hotel_agent", "hotels")}
    planned_flight = next((day["flight"] for day in days if day.get("flight")), None)
    flight_price = 0.0
    if planned_flight:
        match = next(
            (
                f
                for f in flights
                if (f.get("airline"), f.get("flight_number"))
                == (planned_flight.get("airline"), planned_flight.get("flight_number"))
            ),
            None,
        )
        flight_price = (match or planned_flight).get("price_inr") or 0.0
    elif flights and not the_plan.get("missing"):
        flight_price = min(f.get("price_inr") or 0.0 for f in flights)
    stays = [
        hotels.get(day["hotel"].get("name")) or day["hotel"].get("cost_per_night") or 0.0
        for day in days
        if day.get("hotel")
    ]
    activities = sum((slot or {}).get("cost") or 0.0 for day in days for _, slot in slots(day))
    nights = len(stays)
    start = trip.get("start_date")
    estimate = estimate_budget(
        BudgetInput(
            flights=flight_price,
            hotels=sum(stays) / nights if nights else 0.0,
            nights=nights,
            days=max(1, len(days)),
            daily_spend=activities / len(days) if days else 0.0,
            group_size=min(9, max(1, trip.get("group_size") or 1)),
            destination=trip.get("destination"),
            month=date.fromisoformat(start).month if start else None,
        )
    )
    total = getattr(estimate, "total", None)
    if total is None:  # a ToolError
        raise ValueError(f"estimate_budget refused: {estimate}")
    return {
        "total": total,
        "flights": flight_price,
        "nightly": sum(stays) / nights if nights else 0.0,
        "nights": nights,
        "activities": activities,
    }


# ── Hallucination ───────────────────────────────────────────────────────────


def hallucination(scenario: Scenario, evidence: dict) -> Measure:
    m = Measure(HALLUCINATION)
    the_plan = plan(evidence) or {}
    places = {a.get("name") for a in found_by(evidence, "activities_agent", "attractions")}
    hotels = {}
    for h in found_by(evidence, "hotel_agent", "hotels"):
        hotels.setdefault(h.get("name"), h.get("price_per_night_inr"))
    flights = {
        (f.get("airline"), f.get("flight_number")): f.get("price_inr")
        for f in found_by(evidence, "flight_agent", "flights")
    }

    invented_places, invented_hotels, wrong_prices = set(), set(), set()
    for day in the_plan.get("days") or []:
        for _, slot in slots(day):
            if slot and slot["activity"] != FREE_TIME and slot["activity"] not in places:
                invented_places.add(slot["activity"])
        if hotel := day.get("hotel"):
            name = hotel.get("name")
            if name not in hotels:
                invented_hotels.add(name)
            elif not _same_price(hotel.get("cost_per_night"), hotels[name]):
                wrong_prices.add(
                    f"{name} at {_inr(hotel.get('cost_per_night'))} a night (found at {_inr(hotels[name])})"
                )
        if flight := day.get("flight"):
            key = (flight.get("airline"), flight.get("flight_number"))
            if key not in flights:
                m.found(1, f"flight {key[0]} {key[1]} is not among the flights found")
            elif not _same_price(flight.get("price_inr"), flights[key]):
                wrong_prices.add(f"flight {key[1]} at {_inr(flight.get('price_inr'))} (found at {_inr(flights[key])})")
    if invented_places:
        m.found(1, f"places not in the attractions found: {sorted(invented_places)}")
    if invented_hotels:
        m.found(1, f"hotels not in the hotels found: {sorted(invented_hotels)}")
    if wrong_prices:
        m.found(3, f"prices not as found: {sorted(wrong_prices)}")
    m.facts.append(
        f"Every place, hotel and flight in the plan was compared by name with the {len(places)} attractions, "
        f"{len(hotels)} hotels and {len(flights)} flights the searches returned: "
        + ("none is missing from them." if not (invented_places or invented_hotels) else "see the problems.")
    )
    if the_plan.get("missing"):
        m.facts.append(
            "The plan itself says what it could not find: "
            + "; ".join(str(gap.get("search")) for gap in the_plan["missing"])
        )
    if the_plan.get("local_intelligence"):
        m.facts.append(
            "The plan's local_intelligence section is advice from a model's own knowledge, by design, and is shown to "
            "the traveller as advice to check locally: it is not source data and is not judged as hallucination."
        )
    return m


# ── Completeness ────────────────────────────────────────────────────────────


def completeness(scenario: Scenario, evidence: dict) -> Measure:
    m = Measure(COMPLETENESS)
    trip = evidence.get("trip") or {}
    the_plan = plan(evidence) or {}
    days = the_plan.get("days") or []
    expected = _trip_dates(trip) if trip.get("start_date") else []
    planned = [day.get("date") for day in days]
    missing = [d for d in expected if d not in planned]
    repeated = sorted({d for d in planned if planned.count(d) > 1})
    m.facts.append(
        f"The trip is {len(expected)} days ({trip.get('start_date')} to {trip.get('end_date')}); the plan has {len(days)}."
    )
    if missing:
        m.found(1, f"no plan for {missing}")
    if repeated:
        m.found(1, f"more than one plan for {repeated}")

    used = {slot["activity"] for day in days for _, slot in slots(day) if slot}
    found = [a.get("name") for a in latest_found(evidence, "activities_agent", "attractions")]
    unused = [name for name in found if name not in used]
    empty = [(day.get("day"), name) for day in days for name, slot in slots(day) if slot is None]
    lines = []
    for day in days:
        filled = [f"{name}: {slot['activity']}" for name, slot in slots(day) if slot]
        lines.append(f"day {day.get('day')} ({day.get('date')}): " + ("; ".join(filled) or "nothing"))
        if not filled:
            m.found(2, f"day {day.get('day')} has nothing in it, not even free time")
    m.facts.append("Slots: " + " | ".join(lines))
    m.facts.append(
        f"The attractions search found {len(found)} places; {len(unused)} are not in the plan"
        + (f": {unused}" if unused else ".")
    )
    if empty and unused:
        m.found(3, f"{len(empty)} slots left empty while {len(unused)} places found were not used")

    hotels_found = bool(latest_found(evidence, "hotel_agent", "hotels"))
    no_night = [day.get("day") for day in days[:-1] if not day.get("hotel")]
    if hotels_found and no_night:
        m.found(3, f"no hotel on night(s) of day {no_night}, though hotels were found")
    if days and days[-1].get("hotel"):
        m.facts.append("The last day has a hotel night, though it is the day of departure.")

    for member in trip.get("group_members") or []:
        if not member.get("interests"):
            continue
        suited = [
            slot["activity"]
            for day in days
            for _, slot in slots(day)
            if slot and member["name"] in (slot.get("suits") or [])
        ]
        m.facts.append(f"{member['name']} ({', '.join(member['interests'])}): {len(suited)} stops suit them")
        if not suited:
            m.found(3, f"nothing in the plan suits {member['name']}, who enjoys {member['interests']}")
    return m


# ── Refinement accuracy ─────────────────────────────────────────────────────


def sections(the_plan: dict) -> dict:
    days = the_plan.get("days") or []
    flight = next((day["flight"] for day in days if day.get("flight")), None) or {}
    return {
        "dates": [day.get("date") for day in days],
        "flight": (flight.get("airline"), flight.get("flight_number"), flight.get("price_inr")),
        "hotel": sorted({day["hotel"].get("name") for day in days if day.get("hotel")}),
        "activities": [
            (day.get("day"), name, (slot or {}).get("activity")) for day in days for name, slot in slots(day)
        ],
    }


def refinement(scenario: Scenario, evidence: dict) -> Measure:
    m = Measure(REFINEMENT)
    classified = latest(graded_rows(evidence), "refinement_classifier")
    read_as = ((classified or {}).get("output") or {}).get("refinement_type")
    m.facts.append(f"The request was: “{scenario.change}”. The planner read it as {read_as!r}.")
    if scenario.refinement_type and read_as != scenario.refinement_type:
        m.found(2, f"the change was read as {read_as!r}, not {scenario.refinement_type!r}")

    before, after = previous_plan(evidence), plan(evidence)
    if not before or not after:
        m.found(0, "there is no earlier plan to compare with" if after else "no new plan was saved")
        return m
    was, now = sections(before), sections(after)
    changed_slots = [(b, a) for b, a in zip(was["activities"], now["activities"], strict=False) if b[2] != a[2]]
    changed = {
        "dates": was["dates"] != now["dates"],
        "flight": was["flight"] != now["flight"],
        "hotel": was["hotel"] != now["hotel"],
        "activities": bool(changed_slots) or len(was["activities"]) != len(now["activities"]),
    }
    m.facts.append("Changed: " + ", ".join(f"{part} {'yes' if did else 'no'}" for part, did in changed.items()))
    m.facts.append(f"Hotel before {was['hotel']}, after {now['hotel']}.")
    m.facts.append(f"Flight before {was['flight']}, after {now['flight']}.")
    m.facts.append(
        f"{len(changed_slots)} of {len(now['activities'])} activity slots changed: "
        + "; ".join(f"day {b[0]} {b[1]}: {b[2]} → {a[2]}" for b, a in changed_slots[:12])
    )
    m.facts.append(f"Total {_inr(before.get('total_cost'))} before, {_inr(after.get('total_cost'))} after.")
    kinds_before = Counter(_categories(before))
    kinds_after = Counter(_categories(after))
    m.facts.append(f"Kinds of stop before {dict(kinds_before)}, after {dict(kinds_after)}.")

    wanted = scenario.changes
    if wanted and not changed.get(wanted):
        m.found(1, f"the {wanted} is as it was: the change asked for was not made")
    if changed["dates"]:
        m.found(1, "the trip's dates changed")
    for part in ("flight", "hotel"):
        if part != wanted and changed[part]:
            m.found(2, f"the {part} changed, though only the {wanted} was asked about")
    if wanted != "activities" and changed["activities"]:
        m.found(
            3 if len(changed_slots) <= 2 else 2,
            f"{len(changed_slots)} activity slots changed, though only the {wanted} was asked about",
        )
    return m


def _categories(the_plan: dict) -> list[str]:
    return [slot.get("category") or "free time" for day in the_plan.get("days") or [] for _, slot in slots(day) if slot]


MEASURES = {
    TOOLS: tools,
    BUDGET: budget,
    HALLUCINATION: hallucination,
    COMPLETENESS: completeness,
    REFINEMENT: refinement,
}


def measure(scenario: Scenario, evidence: dict) -> dict[str, Measure]:
    """Every criterion this scenario is graded on, measured. Only called for a scenario that ended as it should."""
    criteria = scenario.criteria
    if scenario.expect == "plan or reason" and evidence.get("outcome") != "plan":
        criteria = ()
    if scenario.expect == "conflict" and evidence.get("outcome") == "conflict":
        criteria = (TOOLS, BUDGET)
    return {criterion: MEASURES[criterion](scenario, evidence) for criterion in criteria}
