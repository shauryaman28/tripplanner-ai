"""A demo account with twelve planned trips — to see similar trips and search without planning a dozen.

    python scripts/seed_demo_trips.py        # needs Postgres up, migrations applied, GOOGLE_API_KEY in .env

It creates (or resets) one account and gives it the twelve itineraries of the
embedding experiment (scripts/embedding_experiment.py): four beach trips, two
in the mountains, three heritage, two spiritual, one nature. Each is embedded
by the real model, exactly as a planned trip is, so what the page then shows is
what pgvector finds.

    sign in as   demo@example.com / demo-trips-123

Only that account's trips are touched: run it again to start afresh. The trips
have no flights and no map coordinates — they are plans to compare, not to book.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src" / "backend"), str(ROOT)]

from embedding_experiment import TRIPS, itinerary  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.embedding import Embedding  # noqa: E402
from app.models.itinerary import Itinerary  # noqa: E402
from app.models.trip import Trip, TripStatus  # noqa: E402
from app.models.user import User  # noqa: E402
from src.ai.embeddings.embedder import PENDING_RETRY_MODEL  # noqa: E402
from src.ai.utils.embeddings import generate_embeddings  # noqa: E402

DEMO_EMAIL, DEMO_PASSWORD = "demo@example.com", "demo-trips-123"
# The embedding API's free tier allows a few dozen texts a minute: what it turns away is tried again.
ROUNDS, WAIT_S = 6, 30


def plan(trip: tuple) -> tuple[dict, dict]:
    """A corpus trip as (the trip row's fields, a structured_data the trip page can show)."""
    _name, _kind, destination, month, cost, interests, _places = trip
    _fields, data = itinerary(trip)
    start = date(date.today().year + 1, month, 10)
    nights = len(data["days"]) - 1
    per_night = round(cost * 0.45 / max(nights, 1), -2)
    rating = 7.0
    for number, day in enumerate(data["days"]):
        day["date"] = str(start + timedelta(days=number))
        day["flight"] = None
        day["hotel"] = {"name": f"Hotel {destination}", "cost_per_night": per_night} if number < nights else None
        for slot in ("morning", "afternoon", "evening"):
            stop = day.setdefault(slot, None)
            if stop:
                # the first place of a trip is its best known: it is what the trip is named by
                stop.update(cost=0, lat=None, lng=None, rating=rating if stop.get("category") else None)
                rating = 2.0
    data.update(total_cost=float(cost), currency="INR", local_intelligence=None)
    row = {
        "destination": destination,
        "start_date": start,
        "end_date": start + timedelta(days=nights),
        "budget": float(round(cost * 1.2, -3)),
        "group_size": 2,
        "interests": interests,
        "status": TripStatus.COMPLETED,
    }
    return row, data


async def pending(user_id) -> list:
    """The demo account's itineraries whose embeddings the model has not made yet."""
    async with AsyncSessionLocal() as db:
        query = (
            select(Embedding.itinerary_id)
            .join(Itinerary, Itinerary.id == Embedding.itinerary_id)
            .join(Trip, Trip.id == Itinerary.trip_id)
            .where(Trip.user_id == user_id, Embedding.embedding_model == PENDING_RETRY_MODEL)
        )
        return list((await db.execute(query)).scalars().all())


async def main() -> None:
    if not settings.GOOGLE_API_KEY:
        sys.exit("GOOGLE_API_KEY is not set (.env): the trips could be created but not embedded.")

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == DEMO_EMAIL))).scalars().first()
        if user is None:
            user = User(email=DEMO_EMAIL, hashed_password=hash_password(DEMO_PASSWORD))
            db.add(user)
            await db.commit()
        await db.execute(delete(Trip).where(Trip.user_id == user.id))  # itineraries and embeddings go with them
        await db.commit()

        itineraries = []
        for trip in TRIPS:
            row, data = plan(trip)
            saved = Trip(user_id=user.id, **row)
            db.add(saved)
            await db.commit()
            saved_plan = Itinerary(trip_id=saved.id, structured_data=data, total_cost=data["total_cost"])
            db.add(saved_plan)
            await db.commit()
            itineraries.append(saved_plan.id)
        user_id = user.id

    print(f"{len(itineraries)} trips saved for {DEMO_EMAIL}. Embedding them (two texts each)…")
    waiting = itineraries
    for attempt in range(ROUNDS):
        for itinerary_id in waiting:
            await generate_embeddings(itinerary_id)  # never raises: what fails is left as pending_retry
        waiting = await pending(user_id)
        if not waiting:
            break
        print(f"  {len(waiting)} still to embed (the model's rate limit) — again in {WAIT_S} s")
        await asyncio.sleep(WAIT_S)

    if waiting:
        print(
            f"{len(waiting)} trips are not embedded yet: restart the backend and it will finish them (startup recovery)."
        )
    print(f"Done. Sign in as {DEMO_EMAIL} / {DEMO_PASSWORD}, open a trip, and look for “Similar trips”.")


if __name__ == "__main__":
    asyncio.run(main())
