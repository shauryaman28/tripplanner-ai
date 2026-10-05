"""Which text should an itinerary be embedded as, and what counts as a match? — the Phase 23 experiment.

Not used at runtime and not run in CI: it calls the real embedding model.

    python scripts/embedding_experiment.py        # needs GOOGLE_API_KEY in .env

About 230 texts are embedded. The free tier allows a few dozen a minute, so the
run waits out the limit several times: set EMBEDDING_EXPERIMENT_CACHE to a file
and a run that is cut short picks up where it stopped.

Twelve itineraries of five kinds (beach, mountains, heritage, spiritual, nature)
are embedded as each candidate text, and two questions are asked of each:

  similar trips   for every trip, are its three nearest neighbours trips of the
                  same kind?  (GET /trips/{id}/similar)
  search          for a typed query, are the three nearest trips of the kind it
                  asks for?  (GET /trips/search?q=…)

Then, for the text and task types the application uses, the scores themselves:
how alike two trips of the same kind are and two of different kinds
(app.search.SIMILAR_FLOOR), and how a real query's best match compares with a
nonsense one's (SEARCH_FLOOR) and with the rest of its matches (SEARCH_WINDOW).

"summary" is the application's own text (src/ai/embeddings/embedder.py), so
what is measured is what is stored. Results: docs/phase23_build_log.md,
DECISIONS #140–#141.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src" / "backend"), str(ROOT)]

from app.core.config import settings  # noqa: E402
from src.ai.embeddings import embedder  # noqa: E402

# (name, kind, destination, month, total cost in INR, interests, places as (name, category))
TRIPS = [
    (
        "Goa beaches",
        "beach",
        "Goa",
        12,
        42_000,
        ["beach", "food"],
        [
            ("Baga Beach", "beach"),
            ("Calangute Beach", "beach"),
            ("Fort Aguada", "history"),
            ("Anjuna Beach", "beach"),
            ("Basilica of Bom Jesus", "spiritual"),
            ("Dudhsagar Falls", "nature"),
        ],
    ),
    (
        "South Goa",
        "beach",
        "South Goa",
        1,
        35_000,
        ["beach", "nature"],
        [
            ("Palolem Beach", "beach"),
            ("Agonda Beach", "beach"),
            ("Cabo de Rama Fort", "history"),
            ("Butterfly Beach", "beach"),
            ("Colva Beach", "beach"),
        ],
    ),
    (
        "Andaman",
        "beach",
        "Havelock Island",
        2,
        95_000,
        ["beach", "adventure"],
        [
            ("Radhanagar Beach", "beach"),
            ("Elephant Beach", "beach"),
            ("Cellular Jail", "history"),
            ("Kalapathar Beach", "beach"),
            ("Ross Island", "history"),
            ("Neil Island", "beach"),
        ],
    ),
    (
        "Kerala coast",
        "beach",
        "Kovalam",
        11,
        30_000,
        ["beach", "wellness"],
        [
            ("Kovalam Beach", "beach"),
            ("Varkala Beach", "beach"),
            ("Vizhinjam Lighthouse", "sightseeing"),
            ("Poovar Island", "nature"),
            ("Hawa Beach", "beach"),
        ],
    ),
    (
        "Ladakh trek",
        "mountains",
        "Leh",
        7,
        70_000,
        ["trekking", "adventure"],
        [
            ("Markha Valley Trek", "adventure"),
            ("Pangong Lake", "nature"),
            ("Khardung La", "nature"),
            ("Thiksey Monastery", "spiritual"),
            ("Nubra Valley", "nature"),
            ("Stok Kangri Base Camp", "adventure"),
        ],
    ),
    (
        "Manali and Spiti",
        "mountains",
        "Manali",
        6,
        45_000,
        ["mountains", "adventure"],
        [
            ("Solang Valley", "adventure"),
            ("Rohtang Pass", "nature"),
            ("Hadimba Temple", "spiritual"),
            ("Chandratal Lake", "nature"),
            ("Key Monastery", "spiritual"),
            ("Hampta Pass Trek", "adventure"),
        ],
    ),
    (
        "Jaipur",
        "heritage",
        "Jaipur",
        11,
        24_000,
        ["history", "culture"],
        [
            ("Amber Fort", "history"),
            ("Hawa Mahal", "history"),
            ("City Palace", "history"),
            ("Jantar Mantar", "history"),
            ("Nahargarh Fort", "history"),
        ],
    ),
    (
        "Udaipur and Jodhpur",
        "heritage",
        "Udaipur",
        12,
        38_000,
        ["history", "culture"],
        [
            ("City Palace, Udaipur", "history"),
            ("Lake Pichola", "nature"),
            ("Mehrangarh Fort", "history"),
            ("Jaswant Thada", "history"),
            ("Sajjangarh Palace", "history"),
            ("Umaid Bhawan Palace", "history"),
        ],
    ),
    (
        "Hampi",
        "heritage",
        "Hampi",
        1,
        20_000,
        ["history"],
        [
            ("Virupaksha Temple", "spiritual"),
            ("Vittala Temple", "history"),
            ("Lotus Mahal", "history"),
            ("Elephant Stables", "history"),
            ("Matanga Hill", "nature"),
        ],
    ),
    (
        "Varanasi",
        "spiritual",
        "Varanasi",
        11,
        18_000,
        ["temples", "culture"],
        [
            ("Kashi Vishwanath Temple", "spiritual"),
            ("Dashashwamedh Ghat", "spiritual"),
            ("Sarnath", "history"),
            ("Assi Ghat", "spiritual"),
            ("Manikarnika Ghat", "spiritual"),
        ],
    ),
    (
        "Rishikesh and Haridwar",
        "spiritual",
        "Rishikesh",
        3,
        22_000,
        ["temples", "yoga"],
        [
            ("Triveni Ghat", "spiritual"),
            ("Laxman Jhula", "sightseeing"),
            ("Har Ki Pauri", "spiritual"),
            ("Neelkanth Mahadev Temple", "spiritual"),
            ("Parmarth Niketan", "spiritual"),
            ("Mansa Devi Temple", "spiritual"),
        ],
    ),
    (
        "Kerala hills and backwaters",
        "nature",
        "Munnar",
        9,
        55_000,
        ["nature", "wildlife"],
        [
            ("Alleppey Backwaters", "nature"),
            ("Munnar Tea Gardens", "nature"),
            ("Periyar Wildlife Sanctuary", "nature"),
            ("Eravikulam National Park", "nature"),
            ("Athirappilly Falls", "nature"),
            ("Mattupetty Dam", "nature"),
        ],
    ),
]

# What someone would type into the search box, and the kind of trip it asks for.
QUERIES = [
    ("beach under 50k 5 days", "beach"),
    ("trekking in the mountains", "mountains"),
    ("forts and palaces", "heritage"),
    ("temples and ghats", "spiritual"),
    ("somewhere quiet by the sea", "beach"),
    ("wildlife and waterfalls", "nature"),
]
# More queries, for the cut-offs: what is typed, and the trips (by their place in TRIPS) that answer it.
KINDS = [trip[1] for trip in TRIPS]
MORE_QUERIES = [
    ("beach", {at for at, kind in enumerate(KINDS) if kind == "beach"}),
    ("Goa", {0, 1}),
    ("monasteries", {4, 5}),
    ("palaces", {6, 7, 8}),
    ("yoga and temples", {at for at, kind in enumerate(KINDS) if kind == "spiritual"}),
    ("tea gardens", {11}),
    ("snow and high passes", {at for at, kind in enumerate(KINDS) if kind == "mountains"}),
]
# …and what is not a search for a trip at all.
NONSENSE = ["asdfgh qwerty", "quarterly tax return", "how do I reset my password"]

SUMMARY = "summary"  # the candidate the application stores

MODEL = embedder.EMBEDDING_MODEL
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:batchEmbedContents"


def itinerary(trip: tuple) -> tuple[dict, dict]:
    """A trip of the corpus as (the trip's fields, its structured_data) — two places a day, as the builder plans."""
    _name, _kind, destination, month, cost, interests, places = trip
    days = []
    for n in range(0, len(places), 2):
        slots = dict(zip(("morning", "afternoon"), places[n : n + 2]))
        day = {
            "day": n // 2 + 1,
            "date": f"2027-{month:02d}-{10 + n // 2:02d}",
            "hotel": {"name": f"Hotel {destination}"},
        }
        day.update(
            {slot: {"activity": name, "category": category, "cost": 0} for slot, (name, category) in slots.items()}
        )
        days.append(day)
    days.append(
        {
            "day": len(days) + 1,
            "date": f"2027-{month:02d}-{10 + len(days):02d}",
            "morning": {"activity": "Explore the area"},
        }
    )
    fields = {"destination": destination, "total_cost": float(cost), "interests": interests, "travellers": 2}
    return fields, {"days": days, "total_cost": cost, "currency": "INR"}


# Vectors already fetched, by (task type, text) — and kept in a file when EMBEDDING_EXPERIMENT_CACHE names
# one, so that a run cut short by the rate limit picks up where it stopped.
CACHE_FILE = Path(os.environ["EMBEDDING_EXPERIMENT_CACHE"]) if os.getenv("EMBEDDING_EXPERIMENT_CACHE") else None
_cache: dict[str, list[float]] = json.loads(CACHE_FILE.read_text()) if CACHE_FILE and CACHE_FILE.exists() else {}
BATCH = 20


def embed(texts: list[str], task: str | None) -> list[list[float]]:
    """The vectors of `texts`. The free tier counts every text against a per-minute limit: on a 429, wait and go on."""
    missing = [text for text in dict.fromkeys(texts) if f"{task}|{text}" not in _cache]
    for start in range(0, len(missing), BATCH):
        chunk = missing[start : start + BATCH]
        requests = [
            {
                "model": f"models/{MODEL}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": embedder.EMBEDDING_DIM,
                **({"taskType": task} if task else {}),
            }
            for text in chunk
        ]
        while True:
            response = httpx.post(
                URL, headers={"x-goog-api-key": settings.GOOGLE_API_KEY}, json={"requests": requests}, timeout=120
            )
            if response.status_code != 429:
                break
            error = response.json().get("error", {})
            delays = [d.get("retryDelay", "") for d in error.get("details", []) if "retryDelay" in d]
            wait = float(delays[0].rstrip("s")) + 1 if delays else 30.0
            print(f"  rate limited ({error.get('message', '')[:120]}…) — waiting {wait:.0f} s", file=sys.stderr)
            time.sleep(wait)
        response.raise_for_status()
        for text, item in zip(chunk, response.json()["embeddings"]):
            _cache[f"{task}|{text}"] = item["values"]
        if CACHE_FILE:
            CACHE_FILE.write_text(json.dumps(_cache))
    return [_cache[f"{task}|{text}"] for text in texts]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def nearest(query: list[float], vectors: list[list[float]], skip: int | None = None) -> list[tuple[float, int]]:
    scored = [(cosine(query, vector), at) for at, vector in enumerate(vectors) if at != skip]
    return sorted(scored, reverse=True)


def budget_word(cost: float) -> str:
    return "budget" if cost < 30_000 else "mid-range" if cost < 80_000 else "luxury"


def phase14_summary(fields: dict, data: dict) -> str:
    """The summary Phase 14 stored: destination, length, cost and a budget word first, then five place names."""
    names, _ = embedder.places_and_kinds(data)
    cost = int(fields["total_cost"])
    return f"{fields['destination']} {len(data['days'])} days {cost:,} INR {budget_word(cost)}. Top activities: {', '.join(names[:5])}."


def summary(fields: dict, data: dict) -> str:
    """The application's summary: what kind of trip it is, and nothing about its size."""
    return embedder.build_summary_text(fields["destination"], fields["interests"], data)


def summary_with_numbers(fields: dict, data: dict) -> str:
    """The same, with the trip's size said after it."""
    cost = int(fields["total_cost"])
    return f"{summary(fields, data)} {len(data['days'])} days, {budget_word(cost)}, about {cost:,} INR."


CANDIDATES = {
    "full text": lambda fields, data: embedder.build_full_text(data),
    "summary (Phase 14)": phase14_summary,
    "summary, with numbers": summary_with_numbers,
    SUMMARY: summary,
}
TASKS = ((None, None), (embedder.DOCUMENT, embedder.QUERY), ("SEMANTIC_SIMILARITY", "SEMANTIC_SIMILARITY"))


def compare_texts(texts: dict[str, list[str]]) -> None:
    """Every candidate text under every pair of task types: who are a trip's neighbours, and a query's?"""
    print(
        f"\n{'text / task type of a stored text':52} similar: same kind in top 3   Goa nearer Andaman than Ladakh by   search: right in top 3"
    )
    for name, candidates in texts.items():
        for doc_task, query_task in TASKS:
            vectors = embed(candidates, doc_task)
            queries = embed([query for query, _ in QUERIES], query_task)

            hits = possible = 0
            for at, vector in enumerate(vectors):
                top = [KINDS[other] for _, other in nearest(vector, vectors, skip=at)[:3]]
                hits += top.count(KINDS[at])
                possible += min(3, KINDS.count(KINDS[at]) - 1)
            # the roadmap's own case: is a Goa beach trip nearer the Andaman beaches than the Ladakh trek?
            goa, andaman, ladakh = (vectors[at] for at in (0, 2, 4))
            margin = cosine(goa, andaman) - cosine(goa, ladakh)

            found = wanted = 0
            for (_query, kind), vector in zip(QUERIES, queries):
                top = [KINDS[at] for _, at in nearest(vector, vectors)[:3]]
                found += top.count(kind)
                wanted += min(3, KINDS.count(kind))
            label = f"{name} / {doc_task or 'none'}"
            print(f"{label:52} {hits:>2}/{possible:<26} {margin:+.3f}{'':31} {found}/{wanted}")


def measure_cut_offs(stored: list[str]) -> None:
    """The scores behind app/search.py's three numbers, for the text and task types the application uses."""
    vectors = embed(stored, embedder.DOCUMENT)

    same, other = [], []
    for a in range(len(vectors)):
        for b in range(a + 1, len(vectors)):
            (same if KINDS[a] == KINDS[b] else other).append(cosine(vectors[a], vectors[b]))
    print(f"\nTwo stored trips of the same kind  ({len(same)} pairs): {min(same):.3f}–{max(same):.3f}")
    print(f"Two stored trips of different kinds ({len(other)} pairs): {min(other):.3f}–{max(other):.3f}")
    for floor in (0.82, 0.84, 0.86):
        kept, let_in = sum(s >= floor for s in same), sum(s >= floor for s in other)
        print(
            f"  at {floor}: {kept}/{len(same)} of the same kind kept, {let_in}/{len(other)} of different kinds let in"
        )

    queries = [
        (query, {at for at, kind in enumerate(KINDS) if kind == wanted}) for query, wanted in QUERIES
    ] + MORE_QUERIES
    scores = [
        [cosine(vector, stored_vector) for stored_vector in vectors]
        for vector in embed([q for q, _ in queries], embedder.QUERY)
    ]
    nonsense = [
        max(cosine(vector, stored_vector) for stored_vector in vectors) for vector in embed(NONSENSE, embedder.QUERY)
    ]
    best = [max(row) for row in scores]
    print(
        f"\nBest match of a real query ({len(queries)}): {min(best):.3f}–{max(best):.3f}; of a nonsense one ({len(NONSENSE)}): {min(nonsense):.3f}–{max(nonsense):.3f}"
    )
    for window in (0.03, 0.04, 0.05, 0.06):
        right = wrong = missed = 0
        for (_query, wanted), row in zip(queries, scores):
            returned = {at for at, score in enumerate(row) if score >= max(row) - window}
            right, wrong, missed = (
                right + len(returned & wanted),
                wrong + len(returned - wanted),
                missed + len(wanted - returned),
            )
        print(f"  trips within {window} of the best match: {right} right, {wrong} wrong, {missed} missed")


def main() -> None:
    if not settings.GOOGLE_API_KEY:
        sys.exit("GOOGLE_API_KEY is not set (.env)")
    built = [itinerary(trip) for trip in TRIPS]
    texts = {name: [build(fields, data) for fields, data in built] for name, build in CANDIDATES.items()}
    for name, candidates in texts.items():
        print(f"[{name}] {candidates[0]}")
    compare_texts(texts)
    measure_cut_offs(texts[SUMMARY])


if __name__ == "__main__":
    main()
