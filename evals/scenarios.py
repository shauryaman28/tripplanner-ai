"""The fifteen trips the eval asks for (Phase 33), and what a right answer to each looks like.

The roadmap's mix: happy path 3, budget conflict 2, ambiguous query 2, group trip 2, edge cases 4 (very
low budget, unusual destination, largest group, a date in the past), refinement 2.

Dates are counted from the day the suite runs: 45 days out, as the live checks of earlier phases do —
the sandbox flight and hotel providers refuse dates much further away. A scenario's own `offset` keeps
two trips to the same place from being one search.

Each scenario says what it *expects* — the way the planner should end, not the way it happens to:

    plan        an itinerary is saved
    question    the planner asks what the traveller enjoys, and plans once told (`answer`)
    conflict    the planner stops on the budget and offers ways out; with `choice`, one is taken and planned
    refused     the API turns the trip away
    plan or reason   either an itinerary, or a failure the traveller is told the reason for

A budget conflict depends on live fares, which nobody can promise in advance; so a conflict scenario
also counts a plan made inside the budget as right (the report says the conflict was not reached).

A refinement scenario changes the trip another scenario planned (`refines`): the plan it starts from is
that scenario's, graded there. Two plans fewer to make — on Groq's free tier, a minute and a half.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

DAYS_OUT = 45

# What is graded. The first four for every plan; the fifth for a change to one.
TOOLS, BUDGET, HALLUCINATION, COMPLETENESS, REFINEMENT = (
    "tool_correctness",
    "budget_consistency",
    "hallucination",
    "completeness",
    "refinement_accuracy",
)
CRITERIA = (TOOLS, BUDGET, HALLUCINATION, COMPLETENESS, REFINEMENT)
PLAN_CRITERIA = (TOOLS, BUDGET, HALLUCINATION, COMPLETENESS)

EXPECTS = ("plan", "question", "conflict", "refused", "plan or reason")


@dataclass(frozen=True)
class Scenario:
    id: str
    kind: str  # happy path | budget conflict | ambiguous query | group trip | edge case | refinement
    asks: str  # what it asks, in a line — for the report
    expect: str
    destination: str = ""
    nights: int = 3
    budget: float = 0.0
    group_size: int | None = None
    interests: tuple[str, ...] | None = None
    members: tuple[tuple[str, tuple[str, ...]], ...] | None = None  # (name, interests) for each named traveller
    message: str | None = None  # what the traveller types (POST /plan's raw_input)
    answer: str | None = None  # their answer, when asked what they enjoy
    choice: tuple[str, ...] = ()  # the ways out of a budget conflict they would take, best first
    refines: str | None = None  # the scenario whose trip this one changes
    change: str | None = None  # what they ask to be changed
    changes: str | None = None  # the part of the plan that change is about: flight | hotel | activities
    refinement_type: str | None = None  # how the planner should read the change
    days_from_today: int | None = None  # instead of DAYS_OUT: a date that is wrong on purpose
    offset: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)  # what a grader should know about this one

    def __post_init__(self) -> None:
        assert self.expect in EXPECTS, self.expect
        assert (self.refines is None) == (self.change is None), self.id

    @property
    def criteria(self) -> tuple[str, ...]:
        """What can be graded when it ends as expected. A refusal is graded on the refusal alone."""
        if self.expect == "refused":
            return ()
        if self.expect == "conflict" and not self.choice:
            return (TOOLS, BUDGET)  # no plan: the search that found the fares, and the ways out offered
        return (*PLAN_CRITERIA, REFINEMENT) if self.refines else PLAN_CRITERIA

    def dates(self, today: date | None = None) -> tuple[date, date]:
        start = (today or date.today()) + timedelta(
            days=self.days_from_today if self.days_from_today is not None else DAYS_OUT + self.offset
        )
        return start, start + timedelta(days=self.nights)

    def trip(self, today: date | None = None) -> dict:
        """The body of POST /trips."""
        start, end = self.dates(today)
        body: dict = {
            "destination": self.destination,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "budget": self.budget,
        }
        if self.group_size is not None:
            body["group_size"] = self.group_size
        if self.interests is not None:
            body["interests"] = list(self.interests)
        if self.members is not None:
            body["group_members"] = [{"name": name, "interests": list(likes)} for name, likes in self.members]
        return body


SCENARIOS: tuple[Scenario, ...] = (
    # ── happy path ────────────────────────────────────────────────────────────
    Scenario(
        id="happy-jaipur",
        kind="happy path",
        asks="Jaipur, 3 nights for two, ₹70,000 — forts and good food",
        expect="plan",
        destination="Jaipur",
        nights=3,
        budget=70_000,
        group_size=2,
        interests=("history", "food"),
        message="A relaxed long weekend of forts and good food",
    ),
    Scenario(
        id="happy-goa",
        kind="happy path",
        asks="Goa, 4 nights alone, ₹60,000 — beaches and a little nightlife",
        expect="plan",
        destination="Goa",
        nights=4,
        budget=60_000,
        group_size=1,
        interests=("beach", "nightlife"),
        message="Beaches by day and a little nightlife — my first time in Goa",
        offset=2,
    ),
    Scenario(
        id="happy-varanasi",
        kind="happy path",
        asks="Varanasi, 2 nights for two, ₹50,000 — the ghats, the aarti, the food",
        expect="plan",
        destination="Varanasi",
        nights=2,
        budget=50_000,
        group_size=2,
        interests=("culture", "food"),
        message="The ghats, the evening aarti and the food",
        offset=4,
    ),
    # ── budget conflict ───────────────────────────────────────────────────────
    Scenario(
        id="conflict-kochi",
        kind="budget conflict",
        asks="Kochi, 3 nights for two on ₹12,000 — then raise the budget",
        expect="conflict",
        destination="Kochi",
        nights=3,
        budget=12_000,
        group_size=2,
        interests=("food", "culture"),
        choice=("increase_budget",),
        offset=6,
    ),
    Scenario(
        id="conflict-udaipur",
        kind="budget conflict",
        asks="Udaipur, 5 nights for two on ₹15,000 — then a shorter trip, or a cheaper stay",
        expect="conflict",
        destination="Udaipur",
        nights=5,
        budget=15_000,
        group_size=2,
        interests=("history", "nature"),
        choice=("reduce_days", "cheaper_hotel", "increase_budget"),
        offset=8,
    ),
    # ── ambiguous query ───────────────────────────────────────────────────────
    Scenario(
        id="ambiguous-amritsar",
        kind="ambiguous query",
        asks="Amritsar, 2 nights alone, ₹40,000 — and nothing said about what they enjoy",
        expect="question",
        destination="Amritsar",
        nights=2,
        budget=40_000,
        group_size=1,
        answer="Mostly history, and the street food everyone talks about",
        offset=10,
    ),
    Scenario(
        id="ambiguous-udaipur",
        kind="ambiguous query",
        asks="Udaipur, 3 nights for two — 'slow and pretty, we're really not museum people'",
        expect="plan",
        destination="Udaipur",
        nights=3,
        budget=65_000,
        group_size=2,
        message="Something slow and pretty — we're really not museum people",
        offset=12,
        notes=("No interests were given in the form: what they enjoy has to be read from the message.",),
    ),
    # ── group trip ────────────────────────────────────────────────────────────
    Scenario(
        id="group-goa-friends",
        kind="group trip",
        asks="Goa, 3 nights, four friends who like different things, ₹1,20,000",
        expect="plan",
        destination="Goa",
        nights=3,
        budget=120_000,
        members=(
            ("Aman", ("beach", "nightlife")),
            ("Bela", ("food",)),
            ("Chirag", ("history",)),
            ("Dia", ()),
        ),
        offset=14,
        notes=("Dia named no interests: happy with whatever the others choose.",),
    ),
    Scenario(
        id="group-kochi-family",
        kind="group trip",
        asks="Kochi, 3 nights, a family of three, ₹1,20,000",
        expect="plan",
        destination="Kochi",
        nights=3,
        # ₹90,000 at first: in December's peak the flights for three were ₹46,443 and the planner priced the trip
        # as asked at ₹94,100 — it stopped and offered three ways out that fit, which was right. This scenario is
        # about a group's plan, not a conflict (docs/evals/baseline/group-kochi-family-at-90000.json).
        budget=120_000,
        members=(("Asha", ("museums",)), ("Ravi", ("food",)), ("Mira", ("nature",))),
        offset=16,
    ),
    # ── edge cases ────────────────────────────────────────────────────────────
    Scenario(
        id="edge-tiny-budget",
        kind="edge case",
        asks="Goa, 4 nights for two on ₹3,000",
        expect="conflict",
        destination="Goa",
        nights=4,
        budget=3_000,
        group_size=2,
        interests=("beach",),
        offset=18,
        notes=("₹3,000 cannot pay for two flights to Goa: the right answer is to stop and offer ways out.",),
    ),
    Scenario(
        id="edge-unusual-place",
        kind="edge case",
        asks="Ziro, Arunachal Pradesh — a valley with no airport",
        expect="plan or reason",
        destination="Ziro",
        nights=3,
        budget=60_000,
        group_size=2,
        interests=("nature", "culture"),
        message="The Ziro valley — rice fields and the Apatani villages",
        offset=20,
        notes=(
            "Ziro has no airport. A plan that says what it could not find is right; so is a refusal that says why. "
            "A flight, hotel or place that was not found is not.",
        ),
    ),
    Scenario(
        id="edge-largest-group",
        kind="edge case",
        asks="Jaipur, 2 nights for nine — the most the planner takes",
        expect="plan",
        destination="Jaipur",
        nights=2,
        budget=300_000,
        group_size=9,
        interests=("history", "food"),
        offset=22,
    ),
    Scenario(
        id="edge-past-date",
        kind="edge case",
        asks="Goa, starting yesterday",
        expect="refused",
        destination="Goa",
        nights=3,
        budget=50_000,
        group_size=2,
        interests=("beach",),
        days_from_today=-1,
    ),
    # ── refinement ────────────────────────────────────────────────────────────
    Scenario(
        id="refine-nicer-hotel",
        kind="refinement",
        asks="happy-jaipur, then: 'Could we stay somewhere nicer? A better hotel please.'",
        expect="plan",
        refines="happy-jaipur",
        change="Could we stay somewhere nicer? A better hotel please.",
        changes="hotel",
        refinement_type="targeted_hotel",
    ),
    Scenario(
        id="refine-more-food",
        kind="refinement",
        asks="happy-varanasi, then: 'Fewer temples please — more food and markets instead.'",
        expect="plan",
        refines="happy-varanasi",
        change="Fewer temples please — more food and markets instead.",
        changes="activities",
        refinement_type="targeted_activities",
    ),
)

BY_ID = {scenario.id: scenario for scenario in SCENARIOS}


def in_order(chosen: list[Scenario] | tuple[Scenario, ...]) -> list[Scenario]:
    """The order they are run in: a refinement after the plan it changes, everything else as listed."""
    ids = {scenario.id for scenario in chosen}
    missing = [s.refines for s in chosen if s.refines and s.refines not in ids]
    if missing:
        raise ValueError(f"a refinement needs the plan it changes: add {', '.join(sorted(set(missing)))}")
    return sorted(chosen, key=lambda s: (SCENARIOS.index(BY_ID[s.refines]) + 0.5) if s.refines else SCENARIOS.index(s))


def pick(ids: list[str] | None) -> list[Scenario]:
    """The scenarios named (and the plans a named refinement needs), or all fifteen."""
    if not ids:
        return list(SCENARIOS)
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        raise ValueError(f"no such scenario: {', '.join(unknown)} — there are: {', '.join(BY_ID)}")
    chosen = {i: BY_ID[i] for i in ids}
    for scenario in list(chosen.values()):
        if scenario.refines:
            chosen.setdefault(scenario.refines, BY_ID[scenario.refines])
    return in_order(list(chosen.values()))
