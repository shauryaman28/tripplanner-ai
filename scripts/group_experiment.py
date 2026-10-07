"""What four travellers who want different things get from the attraction search — together, and one by one.

    python scripts/group_experiment.py                      # five destinations, a four-day trip
    python scripts/group_experiment.py Goa Manali --days 6

The roadmap's group (Phase 25): Asha — beach, food; Ben — history, culture;
Chitra — adventure; Dev — spa, relaxation. For each destination this runs the
real attraction search (the MCP server, OpenTripMap; needs OPENTRIPMAP_API_KEY)
and prints three things:

  together     one search with all seven interests, as a group was searched
               before Phase 25: how many of its ten places suit each traveller
  one by one   a search per traveller, merged and taken in turns (src/ai/group.py):
               what each search found, what it found nothing for, and how many
               of the places the plan is made from suit each traveller
  a plan       those places put two a day into a plan, in order, then repaired
               (group.rebalance): the stops each traveller has in every two days

No model is called: the plan here is the simplest one a builder could write.
What a real builder writes is checked by the same `rebalance`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src" / "backend"), str(ROOT)]

from src.ai import group  # noqa: E402
from src.ai.agents.activities_agent import ActivitiesAgent  # noqa: E402
from src.ai.itinerary import SLOTS  # noqa: E402
from src.ai.mcp_client.client import close_session  # noqa: E402

GROUP = [
    {"name": "Asha", "interests": ["beach", "food"]},
    {"name": "Ben", "interests": ["history", "culture"]},
    {"name": "Chitra", "interests": ["adventure"]},
    {"name": "Dev", "interests": ["spa", "relaxation"]},
]
DESTINATIONS = ["Goa", "Jaipur", "Manali", "Kochi", "Rishikesh"]


def per_member(attractions: list[dict]) -> str:
    return "   ".join(f"{m['name']} {sum(group.suits(a, m) for a in attractions)}" for m in GROUP)


def simple_plan(attractions: list[dict], days: int) -> dict:
    """Two stops a day from the top of the list, the evening free — what the test stand-in writes too."""
    names = [a["name"] for a in attractions]

    def slot(index: int) -> dict | None:
        return {"activity": names[index], "cost": 0.0} if index < len(names) else None

    return {
        "days": [
            {"day": n + 1, "morning": slot(2 * n), "afternoon": slot(2 * n + 1), "evening": None} for n in range(days)
        ],
        "total_cost": 0.0,
    }


def by_window(plan: dict, attractions: list[dict]) -> list[str]:
    suited = {a["name"]: a.get(group.SUITS, []) for a in attractions}
    lines = []
    for window in group.windows(len(plan["days"])):
        stops = [plan["days"][i][s]["activity"] for i in window for s in SLOTS if plan["days"][i].get(s)]
        counts = "   ".join(f"{m['name']} {sum(m['name'] in suited.get(stop, []) for stop in stops)}" for m in GROUP)
        days = "–".join(str(plan["days"][i]["day"]) for i in (window[0], window[-1]))
        lines.append(f"days {days}: {counts}")
    return lines


async def look(destination: str, days: int) -> None:
    print(f"\n== {destination}, {days} days ==")
    agent = ActivitiesAgent()

    together = await agent.run({"destination": destination, "interests": group.all_interests(GROUP), "limit": 10})
    if together.get("error"):
        print(f"  together     failed: {together['error']['code']} — {together['error']['error']}")
    else:
        print(f"  together     {len(together['attractions']):>2} places:  {per_member(together['attractions'])}")

    apart = await agent.run(
        {"destination": destination, "interests": group.all_interests(GROUP), "group_members": GROUP, "days": days}
    )
    if apart.get("error"):
        print(f"  one by one   failed: {apart['error']['code']} — {apart['error']['error']}")
        return
    for search in apart["group_searches"]:
        nothing = f"   nothing for: {', '.join(search['nothing_for'])}" if search["nothing_for"] else ""
        print(f"  one by one   {search['name']:<7} found {search['found']}{nothing}")
    attractions = apart["attractions"]
    print(f"               {len(attractions):>2} places:  {per_member(attractions)}")
    for place in attractions:
        print(f"                 {place['group_score']:.2f}  {place['name'][:38]:<38} {', '.join(place[group.SUITS])}")

    plan = simple_plan(attractions, days)
    moved = group.rebalance(plan, attractions, GROUP)
    for line in by_window(plan, attractions):
        print(f"  a plan       {line}")
    if moved:
        print(f"               repaired: {'; '.join(moved)}")
    result = group.summary(plan, attractions, GROUP, apart["group_searches"])
    print(f"               balanced: {result['balanced']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="The roadmap's four travellers and the attraction search.")
    parser.add_argument("destinations", nargs="*", default=DESTINATIONS)
    parser.add_argument("--days", type=int, default=4)
    args = parser.parse_args()
    try:
        for destination in args.destinations:
            await look(destination, args.days)
    finally:
        await close_session()


if __name__ == "__main__":
    asyncio.run(main())
