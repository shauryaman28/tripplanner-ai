"""A group trip: whose interests a place answers, and a plan that gives everyone their share (Phase 25).

A trip can name its travellers and what each enjoys:

    group_members = [{"name": "Asha", "interests": ["beach", "food"]},
                     {"name": "Ben",  "interests": ["history", "culture"]}, …]

Searched as one traveller with all of their interests, such a group gets the
ten places nearest the centre under the first few of them, and a plan that is
one person's holiday. Here is what makes it everyone's — all of it plain code, with
no model in it, so every part can be tested and none of it can be talked out of:

    suits                      does a place answer one of a member's interests?
    score_activity_for_group   the share of the group a place is for
    merge_for_group            every member's own search, as one list
    pick_for_group             the places a plan is made from: whoever has the fewest is served next
    rebalance                  a written plan, repaired: in every two days, a stop for each member
    fixable_gaps               what `rebalance` would still change (the evaluator's check)
    summary                    who got how many stops, and whose interests found nothing

"A stop for each member in every two days" holds as far as the places found
allow: nobody can be given a spa the provider does not list. What was found
for nobody is said, not hidden (`summary`).
"""

from __future__ import annotations

import copy
import unicodedata
from collections.abc import Iterable

from src.ai.itinerary import FREE_TIME, SLOTS

# On an attraction and on a plan's slot: the names of the members it is for.
SUITS = "suits"

MIN_MEMBERS, MAX_MEMBERS = 2, 9  # 9: the flight search's passenger limit
MAX_NAME_CHARS, MAX_INTERESTS, MAX_INTEREST_CHARS = 40, 6, 30
# A name or an interest is a few words, in any script. Nothing that could close a JSON string or
# start a new line: both are shown to the model that writes the plan.
_PUNCTUATION = frozenset(" .,'&()/+-")

# What a member's own search may return — the tool's most: the provider answers nearest-first, and
# with fewer a traveller who likes history gets the three sights by the station and never the fort
# on the hill — and how many places a group's plan is then made from.
MEMBER_SEARCH_LIMIT = 10
MIN_GROUP_ATTRACTIONS, MAX_GROUP_ATTRACTIONS = 10, 20


# ── Members ────────────────────────────────────────────────────────────────


def is_words(text: str, limit: int) -> bool:
    """A short piece of plain text: what a name or an interest may be.

    Letters, their marks (the vowel signs of Devanagari are marks, not letters)
    and digits of any script, spaces and a little punctuation — starting with a
    letter or a digit.
    """
    if not 0 < len(text) <= limit or not text[0].isalnum():
        return False
    return all(char in _PUNCTUATION or unicodedata.category(char)[0] in "LMN" for char in text)


def clean_members(raw: object) -> list[dict]:
    """Members as the rest of the code may rely on them: {"name": str, "interests": [str, …]}.

    Anything that is not that shape is left out — the API has refused it long
    before (app/schemas/trip.py); this is for state read back from storage.
    """
    members: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            continue
        interests = [i.strip() for i in entry.get("interests") or [] if isinstance(i, str) and i.strip()]
        members.append({"name": entry["name"].strip(), "interests": interests})
    return members


def profiles(members: Iterable[dict] | None) -> list[dict]:
    """The members who said what they enjoy — the ones a plan can be balanced between."""
    return [member for member in clean_members(list(members or [])) if member["interests"]]


def is_group(members: Iterable[dict] | None) -> bool:
    """Whether there is anything to balance: at least two members with interests of their own."""
    return len(profiles(members)) >= 2


def all_interests(members: Iterable[dict] | None) -> list[str]:
    """Every member's interests, each once, in the order they are first named."""
    seen: dict[str, None] = {}
    for member in clean_members(list(members or [])):
        for interest in member["interests"]:
            seen.setdefault(interest, None)
    return list(seen)


# ── Whose interests a place answers ────────────────────────────────────────


def _forms(interest: str) -> set[str]:
    """An interest and its singular, as the attraction search reads it: "beaches" is "beach"."""
    key = interest.strip().lower()
    return {key, key.removesuffix("es"), key.removesuffix("s")}


def suits(activity: dict, member: dict) -> bool:
    """Whether a place answers one of a member's interests.

    A place carries the interests it was found under (the attraction search's
    `interests`). It suits a member when one of those is theirs — so a fort
    found for Ben's "history" also suits Chitra, who said "history" too.
    """
    found_under: set[str] = set()
    for interest in activity.get("interests") or []:
        found_under |= _forms(interest)
    return any(_forms(interest) & found_under for interest in member.get("interests") or [])


def nothing_for(member: dict, attractions: list[dict]) -> list[str]:
    """The interests of a member that none of these places was found under."""
    answered: set[str] = set()
    for attraction in attractions:
        for interest in attraction.get("interests") or []:
            answered |= _forms(interest)
    return [interest for interest in member.get("interests") or [] if not _forms(interest) & answered]


def tag_for_group(attractions: list[dict], members: list[dict]) -> list[dict]:
    """Say on each place who it suits and what it scores for the group. Changes the places; returns the list."""
    for attraction in attractions:
        attraction[SUITS] = [member["name"] for member in members if suits(attraction, member)]
        attraction["group_score"] = round(score_activity_for_group(attraction, members), 3)
    return attractions


def score_activity_for_group(activity: dict, members: list[dict]) -> float:
    """Average relevance across all members: the share of the group this place is for, 0 to 1.

    A member's relevance is 1 when the place answers one of their interests and
    0 when it does not, so a beach that two of four travellers asked for is 0.5.
    """
    if not members:
        return 0.0
    return sum(suits(activity, member) for member in members) / len(members)


def _popularity(activity: dict) -> float:
    """OpenTripMap's rate without the heritage offset: 1–3, and 5–7 for the same scale on a heritage site."""
    rating = activity.get("rating")
    return float(rating) % 4 if isinstance(rating, (int, float)) and not isinstance(rating, bool) else 0.0


def merge_for_group(found: list[tuple[dict, list[dict]]], members: list[dict]) -> list[dict]:
    """One list from every member's own search, best for the group first.

    `found` is [(member, the attractions their search returned), …]. A place
    two searches returned is kept once, with everything it was found under.
    Every place then says who it suits and scores as the share of the group
    that is — the ranking is by that score, then by popularity.
    """
    by_name: dict[str, dict] = {}
    for _member, attractions in found:
        for attraction in attractions:
            name = attraction.get("name")
            if not name:
                continue
            kept = by_name.setdefault(name, {**attraction, "interests": []})
            kept["interests"] += [i for i in attraction.get("interests") or [] if i not in kept["interests"]]

    pool = tag_for_group(list(by_name.values()), members)
    # sorted() is stable: among equals, the order the searches gave them in stands
    return sorted(pool, key=lambda a: (-a["group_score"], -_popularity(a)))


def attraction_count(members: list[dict], days: int) -> int:
    """How many places a group's plan is made from: one for each member in each two days, and one over."""
    wanted = len(profiles(members)) * (len(windows(days)) + 1)
    return max(MIN_GROUP_ATTRACTIONS, min(MAX_GROUP_ATTRACTIONS, wanted))


def pick_for_group(pool: list[dict], members: list[dict], count: int) -> list[dict]:
    """The `count` places a group's plan is made from, in the order they were taken.

    Ranked by score alone, a pool is whatever most of the group shares and then
    whoever's interests the provider knows best: four popular forts before the
    one waterfall. So the places are taken in turns — each time for the member
    who has the fewest so far, the best-ranked place left that suits them. A
    place that suits two members counts for both.

    The order matters as much as the choice: a plan filled from the top of this
    list, two or three stops a day, has something for everyone in its first days.
    """
    left = list(pool)
    have = {member["name"]: 0 for member in profiles(members)}
    chosen: list[dict] = []
    while left and len(chosen) < count:
        waiting = [name for name in have if any(name in place.get(SUITS, []) for place in left)]
        if not waiting:  # what is left is for nobody in particular
            chosen += left[: count - len(chosen)]
            break
        name = min(waiting, key=lambda member_name: have[member_name])  # min() keeps the first of equals
        place = next(place for place in left if name in place.get(SUITS, []))
        left.remove(place)
        chosen.append(place)
        for served in place.get(SUITS, []):
            if served in have:
                have[served] += 1
    return chosen


# ── A plan with something for everyone ─────────────────────────────────────


def windows(days: int) -> list[list[int]]:
    """The days of a trip in twos, as indexes: [[0, 1], [2, 3]]. An odd last day joins the two before it.

    "At least one stop for each member per two days" is checked window by
    window. A single last day — usually the day of the flight home — could not
    hold a stop for each of four people on its own.
    """
    pairs = [list(range(first, min(first + 2, days))) for first in range(0, days, 2)]
    if len(pairs) > 1 and len(pairs[-1]) == 1:
        last_day = pairs.pop()
        pairs[-1] += last_day
    return pairs


def _stops(days: list[dict], window: list[int]):
    """(day index, slot name, slot) of every real stop in a window — free time is not one."""
    for index in window:
        for slot_name in SLOTS:
            slot = days[index].get(slot_name)
            if slot and slot.get("activity") and slot["activity"] != FREE_TIME:
                yield index, slot_name, slot


def _suited(slot: dict, by_name: dict[str, dict]) -> list[str]:
    """Who a stop is for — from the attraction it is, never from what the plan's writer said."""
    return list((by_name.get(slot["activity"]) or {}).get(SUITS) or [])


def _free_slot(days: list[dict], window: list[int]) -> tuple[int, str] | None:
    """An empty slot in the window — on its emptiest day, earliest in the day."""
    by_day: list[tuple[int, int, str]] = []
    for index in window:
        day = days[index]
        filled = sum(1 for name in SLOTS if day.get(name) and day[name].get("activity") != FREE_TIME)
        free = next((name for name in SLOTS if not day.get(name) or day[name].get("activity") == FREE_TIME), None)
        if free is not None:
            by_day.append((filled, index, free))
    if not by_day:
        return None
    _filled, index, slot_name = min(by_day)
    return index, slot_name


def _spare_stops(
    days: list[dict], window: list[int], by_name: dict[str, dict], rank: dict[str, int]
) -> list[tuple[int, str]]:
    """The stops a window can lose without leaving anyone with none, the easiest to lose first.

    A stop that is for nobody in particular goes first; then those whose
    members all have another stop in these days — the more they have, the
    sooner; among equals, the place that ranks lowest for the group. Empty when
    every stop is somebody's only one.
    """
    stops = list(_stops(days, window))
    count: dict[str, int] = {}
    for _index, _slot_name, slot in stops:
        for name in _suited(slot, by_name):
            count[name] = count.get(name, 0) + 1

    candidates: list[tuple[int, int, int, int]] = []
    for index, slot_name, slot in stops:
        suited = _suited(slot, by_name)
        if all(count[name] > 1 for name in suited):
            spare = min((count[name] for name in suited), default=len(stops) + 1)
            candidates.append((-spare, -rank.get(slot["activity"], len(rank)), index, SLOTS.index(slot_name)))
    return [(index, SLOTS[slot]) for _spare, _rank, index, slot in sorted(candidates)]


def _a_stop_to_bring(
    days: list[dict], elsewhere: list[list[int]], name: str, by_name: dict[str, dict], rank: dict[str, int]
) -> tuple[int, str] | None:
    """A stop of this member's in other days that those days can spare — they have another there."""
    for window in elsewhere:
        for index, slot_name in _spare_stops(days, window, by_name, rank):
            if name in _suited(days[index][slot_name], by_name):
                return index, slot_name
    return None


def rebalance(draft: dict, attractions: list[dict], members: list[dict]) -> list[str]:
    """Repair a plan so that every two days hold a stop for each member. Returns what was changed.

    For every window and every member with nothing in it, a stop is found:
    a place that suits them and is not in the plan yet — or, when there is
    none, one of their stops from other days that have two. It goes into an
    empty slot, or takes the place of a stop that window can spare
    (`_spare_stops`). A place given up is free to be used again. `total_cost`
    follows what was taken out.

    Changes `draft` in place. Every change gives a member a window they did not
    have and takes no window from anyone, so it ends; running it again on its
    own result changes nothing, which `fixable_gaps` relies on.
    """
    group = profiles(members)
    days = draft.get("days") or []
    if len(group) < 2 or not days:
        return []
    by_name = {a["name"]: a for a in attractions if a.get("name")}
    rank = {a["name"]: place for place, a in enumerate(attractions) if a.get("name")}
    every_window = windows(len(days))
    changes: list[str] = []

    progress = True
    while progress:  # a stop given up in one window can be the one another window was missing
        progress = False
        for window in every_window:
            for member in group:
                name = member["name"]
                if any(name in _suited(slot, by_name) for _i, _n, slot in _stops(days, window)):
                    continue

                used = {slot["activity"] for _i, _n, slot in _stops(days, list(range(len(days))))}
                unused = next((a for a in attractions if name in a.get(SUITS, []) and a["name"] not in used), None)
                brought = None
                if unused is None:
                    brought = _a_stop_to_bring(days, [w for w in every_window if w != window], name, by_name, rank)
                    if brought is None:
                        continue  # nothing that suits them is left, here or elsewhere: said in the summary

                free = _free_slot(days, window)
                spare = None if free else next(iter(_spare_stops(days, window, by_name, rank)), None)
                if (where := free or spare) is None:
                    continue  # every stop in these days is somebody's only one

                if brought is not None:  # out of the days it was in…
                    from_day, from_slot = brought
                    stop = days[from_day][from_slot]
                    days[from_day][from_slot] = None
                else:
                    stop = {"activity": unused["name"], "cost": 0.0, "lat": None, "lng": None}
                index, slot_name = where  # …and into these
                before = days[index].get(slot_name)
                if before and before.get("activity") != FREE_TIME:
                    draft["total_cost"] = (draft.get("total_cost") or 0.0) - (before.get("cost") or 0.0)
                days[index][slot_name] = stop

                what = f"{stop['activity']} for {name}"
                if brought is not None:
                    what += f", from day {days[brought[0]].get('day', brought[0] + 1)}"
                if before and before.get("activity") and before["activity"] != FREE_TIME:
                    what += f" instead of {before['activity']}"
                changes.append(f"day {days[index].get('day', index + 1)} {slot_name}: {what}")
                progress = True
    return changes


def fixable_gaps(draft: dict, attractions: list[dict], members: list[dict]) -> list[str]:
    """What `rebalance` would change in this plan — empty for a plan that is as balanced as it can be."""
    return rebalance(copy.deepcopy(draft), attractions, members)


def summary(draft: dict, attractions: list[dict], members: list[dict], searches: list[dict] | None = None) -> dict:
    """Who the plan is for, and how it turned out — saved with the itinerary under "group".

        {"members": [{"name": "Asha", "interests": ["beach", "food"], "stops": 3, "nothing_for": ["food"]}, …],
         "balanced": true}

    `stops` counts the plan's stops that suit the member. `nothing_for` lists
    the interests of theirs that no place was found for (from the activities
    agent's `searches`). `balanced` is the roadmap's rule read off the plan:
    in every two days, a stop for every member who said what they enjoy. It is
    false for a plan that could not do better, too — a member nothing was found
    for has no stop anywhere, and the page says so rather than "balanced".
    """
    by_name = {a["name"]: a for a in attractions if a.get("name")}
    days = draft.get("days") or []
    nothing_for = {search.get("name"): search.get("nothing_for") or [] for search in searches or []}

    stops: dict[str, int] = {}
    for _index, _slot_name, slot in _stops(days, list(range(len(days)))):
        for name in _suited(slot, by_name):
            stops[name] = stops.get(name, 0) + 1

    balanced = all(
        any(member["name"] in _suited(slot, by_name) for _i, _n, slot in _stops(days, window))
        for window in windows(len(days))
        for member in profiles(members)
    )
    return {
        "members": [
            {
                "name": member["name"],
                "interests": member["interests"],
                "stops": stops.get(member["name"], 0),
                "nothing_for": nothing_for.get(member["name"], []),
            }
            for member in clean_members(members)
        ],
        "balanced": balanced,
    }
