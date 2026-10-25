"""Phase 40 — the two emails through the running app, on real Postgres and Redis; migration 009.

RUN_INTEGRATION=1 pytest tests/integration/test_phase40_notifications_integration.py -v

The clock is the test's: `due_reminders(now)` is asked at the hours either side of a trip's reminder. What is
sent is caught at `notifications.send` — the mailer itself is tested in tests/unit/test_phase40_notifications.py.
"""

import asyncio
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import IntegrityError

from app.core import notifications
from app.core.config import settings
from tests.fakes import flight
from tests.integration.test_pipeline_integration import _login, _run_finished, _stack, _until

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

START = date(2030, 1, 10)
LEAVES = datetime(2030, 1, 10, 0, 30, tzinfo=timezone.utc)  # the stand-in flight's 06:00, in India's time


@contextmanager
def outbox():
    """Every email the app sends, caught before it would leave."""
    sent: list[notifications.Email] = []

    async def send(email):
        sent.append(email)
        return "logged", "log", None

    with patch.object(notifications, "send", AsyncMock(side_effect=send)):
        yield sent


async def _rows(db_session, trip_id: str) -> list[tuple]:
    found = await db_session.execute(
        text("SELECT kind, about, status, channel FROM notifications WHERE trip_id = :id ORDER BY created_at"),
        {"id": trip_id},
    )
    return [tuple(row) for row in found]


async def _planned_trip(client, db_session) -> str:
    """A trip of three days from START, planned through the app; returns once its ready email is written down."""
    body = {
        "destination": "Goa",
        "start_date": str(START),
        "end_date": str(START + timedelta(days=2)),
        "budget": 60_000,
        "group_size": 2,
        "interests": ["history"],
    }
    trip_id = (await client.post("/trips", json=body)).json()["id"]
    assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "A long weekend"})).status_code == 202
    await _run_finished(client, trip_id, orchestrator_rows=1)

    async def written():
        rows = await _rows(db_session, trip_id)
        return rows if rows and rows[0][2] != "sending" else None

    await _until(written, f"the ready email of trip {trip_id}")
    return trip_id


async def test_a_trips_first_itinerary_is_emailed_once_with_its_summary_and_link(db_session):
    with outbox() as sent:
        async with _stack(flight_tool=lambda _name, params: [flight(8_200.0, params["date"])]) as (client, _e, _t):
            await _login(client)
            trip_id = await _planned_trip(client, db_session)
            plan = (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]
            again = await notifications.itinerary_ready(uuid.UUID(trip_id))  # a later itinerary's run

    assert again is None and await _rows(db_session, trip_id) == [("itinerary_ready", "first", "logged", "log")]
    (email,) = sent
    assert email.subject == "Your Goa itinerary is ready" and email.to.endswith("@example.com")
    assert email.text.startswith(f"Your trip to Goa, 10 – 12 Jan 2030, is planned: {len(plan['days'])} days")
    assert f"₹{plan['total_cost']:,.0f} in all." in email.text
    assert f"{settings.SITE_URL}/trips/{trip_id}" in email.text


async def test_the_reminder_fires_at_its_hour_once_and_is_counted_from_the_flight(db_session):
    with outbox() as sent:
        async with _stack(flight_tool=lambda _name, params: [flight(8_200.0, params["date"])]) as (client, _e, _t):
            await _login(client)
            trip_id = await _planned_trip(client, db_session)
        sent.clear()  # the ready email

        assert await notifications.due_reminders(LEAVES - timedelta(hours=24, minutes=1)) == []  # not yet
        assert await notifications.due_reminders(LEAVES - timedelta(hours=24)) == [(uuid.UUID(trip_id), "logged")]
        assert await notifications.due_reminders(LEAVES - timedelta(hours=23)) == []  # once
        assert await notifications.due_reminders(LEAVES + timedelta(minutes=1)) == []

    assert await _rows(db_session, trip_id) == [
        ("itinerary_ready", "first", "logged", "log"),
        ("trip_reminder", "2030-01-10", "logged", "log"),
    ]
    (email,) = sent
    assert email.subject == "Your trip to Goa starts tomorrow"
    assert email.text.splitlines()[2] == "Flight 6E-204 leaves DEL at 06:00"


async def test_the_travellers_own_timing_is_kept_and_off_is_off(db_session):
    with outbox() as sent:
        async with _stack(flight_tool=lambda _name, params: [flight(8_200.0, params["date"])]) as (client, _e, _t):
            await _login(client)
            prefs = (await client.get("/users/preferences")).json()
            prefs.pop("updated_at")
            saved = await client.put("/users/preferences", json=prefs | {"reminder_hours_before": 48})
            assert saved.status_code == 200
            early = await _planned_trip(client, db_session)

            assert await notifications.due_reminders(LEAVES - timedelta(hours=49)) == []
            assert await notifications.due_reminders(LEAVES - timedelta(hours=48)) == [(uuid.UUID(early), "logged")]
            assert sent[-1].subject == "Your trip to Goa starts on Thursday 10 January"

            off = await client.put("/users/preferences", json=prefs | {"notification_email": False})
            assert off.status_code == 200
            sent.clear()
            quiet = await _planned_trip(client, db_session)
            assert await notifications.due_reminders(LEAVES - timedelta(hours=24)) == [(uuid.UUID(quiet), "off")]

    assert sent == []  # neither the ready email nor the reminder
    assert await _rows(db_session, quiet) == [
        ("itinerary_ready", "first", "off", None),
        ("trip_reminder", "2030-01-10", "off", None),
    ]


async def test_two_backends_looking_at_once_send_the_reminder_once_even_on_the_day_it_begins(db_session):
    with outbox() as sent:
        async with _stack(flight_tool=lambda _name, params: [flight(8_200.0, params["date"])]) as (client, _e, _t):
            await _login(client)
            trip_id = await _planned_trip(client, db_session)
        sent.clear()
        now = LEAVES - timedelta(hours=3)  # the morning it begins, before the flight: a trip of today is looked at
        both = await asyncio.gather(*(notifications.due_reminders(now) for _ in range(4)))

    assert sorted(both, key=len) == [[], [], [], [(uuid.UUID(trip_id), "logged")]]
    assert len(sent) == 1


def test_migration_009_adds_the_settings_and_the_table_and_comes_back_down():
    config = Config("alembic.ini")
    engine = create_engine(make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2"))

    def columns() -> dict[str, tuple]:
        with engine.begin() as db:
            rows = db.execute(
                text(
                    "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'user_preferences'"
                )
            ).all()
        return {name: (nullable, default) for name, nullable, default in rows}

    def tables() -> set[str]:
        with engine.begin() as db:
            return set(db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())

    assert columns()["notification_email"] == ("NO", "true")
    assert columns()["reminder_hours_before"] == ("NO", "24")
    assert "notifications" in tables()
    user = uuid.uuid4()
    with engine.begin() as db:
        db.execute(
            text("INSERT INTO users (id, email, hashed_password, created_at) VALUES (:id, :email, 'x', now())"),
            {"id": user, "email": f"{user.hex[:12]}@example.com"},
        )
    try:
        command.downgrade(config, "008")
        try:
            assert {"notification_email", "reminder_hours_before"}.isdisjoint(columns())
            assert "notifications" not in tables()
        finally:
            command.upgrade(config, "head")
        # a row written before the migration has the defaults; the limits hold
        with engine.begin() as db:
            db.execute(text("INSERT INTO user_preferences (user_id) VALUES (:id)"), {"id": user})
            got = db.execute(
                text("SELECT notification_email, reminder_hours_before FROM user_preferences WHERE user_id = :id"),
                {"id": user},
            ).one()
        assert tuple(got) == (True, 24)
        for hours in (0, 169):
            with pytest.raises(IntegrityError), engine.begin() as db:
                db.execute(
                    text("UPDATE user_preferences SET reminder_hours_before = :h WHERE user_id = :id"),
                    {"h": hours, "id": user},
                )
        # the same email for the same trip, twice: refused by the table itself
        trip = uuid.uuid4()
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO trips (id, user_id, destination, start_date, end_date, budget, group_size, status, "
                    "created_at) VALUES (:t, :u, 'Goa', '2030-01-10', '2030-01-12', 1000, 1, 'completed', now())"
                ),
                {"t": trip, "u": user},
            )
        once = text(
            "INSERT INTO notifications (id, user_id, trip_id, kind, about, status, created_at) "
            "VALUES (:id, :u, :t, 'trip_reminder', '2030-01-10', 'sending', now())"
        )
        with engine.begin() as db:
            db.execute(once, {"id": uuid.uuid4(), "u": user, "t": trip})
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(once, {"id": uuid.uuid4(), "u": user, "t": trip})
    finally:
        with engine.begin() as db:
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
        engine.dispose()
