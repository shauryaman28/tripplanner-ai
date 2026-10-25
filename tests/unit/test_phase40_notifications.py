"""Phase 40 — the two emails: "Your itinerary is ready", and the reminder before a trip.

The roadmap's acceptance, and where each is held:

  Dev A  - an email is logged (dev) or sent (prod) on planning_complete    → the mailer's tests here; the planning
                                                                              run's, through the app, in
                                                                              tests/integration/test_phase40_…
         - the reminder fires at the right time, with an injectable now()   → `reminder_due` and `departure` here;
                                                                              `due_reminders(now)` on a database
                                                                              in the integration tests
         - no HTTP request waits for an email                               → the planning run's test here
  Dev B  - notification_email false is respected                           → here, and in the integration tests
         - the reminder's timing is configurable and respected              → here (the limits, the window)
         - the settings page renders and saves                              → e2e/settings.spec.ts
"""

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app import preflight
from app.core import notifications
from app.core.config import settings
from app.core.notifications import INDIA, Email
from tests.unit.test_phase16_preferences import _call, _route_session
from tests.unit.test_phase27_production import deployment, providers

TRIP = SimpleNamespace(
    id=uuid.UUID("0f6c5c1e-8a51-4f2e-9a43-1b2c3d4e5f60"),
    user_id=uuid.uuid4(),
    destination="Goa",
    start_date=date(2030, 1, 10),
    end_date=date(2030, 1, 12),
)
USER = SimpleNamespace(email="asha@example.com")
PLAN = {
    "total_cost": 41_250.0,
    "days": [
        {
            "day": 1,
            "date": "2030-01-10",
            "flight": {
                "airline": "6E",
                "flight_number": "6E-204",
                "departure": "2030-01-10T06:00:00",
                "origin": {"code": "DEL"},
            },
            "hotel": {"name": "Goa Grand"},
            "weather": {"condition": "Rain", "temp_high_c": 29.0, "temp_low_c": 24.0},
            "morning": {"activity": "Explore the area"},
            "afternoon": {"activity": "Fort Aguada", "start_time": "13:00", "end_time": "15:00"},
            "evening": {"activity": "Baga Beach", "start_time": "18:00", "end_time": "19:30"},
        },
        {"day": 2, "date": "2030-01-11", "morning": {"activity": "Basilica of Bom Jesus"}},
        {"day": 3, "date": "2030-01-12"},
    ],
}
EMAIL = Email("asha@example.com", "Your Goa itinerary is ready", "the text", "<p>the html</p>")


# ── Sent, or logged ────────────────────────────────────────────────────────


async def test_with_no_key_an_email_is_written_to_the_log_whole_in_development(caplog):
    caplog.set_level(logging.INFO, logger=notifications.__name__)
    with patch.object(settings, "APP_ENV", "development"):
        assert await notifications.send(EMAIL) == ("logged", "log", None)
    assert "Your Goa itinerary is ready" in caplog.text and "the text" in caplog.text
    assert "to [Filtered]" in caplog.text and "asha@" not in caplog.text  # no log line holds an address (Phase 29)


async def test_in_production_with_no_key_the_log_says_only_that_it_was_not_sent(caplog):
    caplog.set_level(logging.INFO, logger=notifications.__name__)
    with patch.object(settings, "APP_ENV", "production"):
        assert await notifications.send(EMAIL) == ("logged", "log", None)
    assert "not sent — no SENDGRID_API_KEY" in caplog.text and "a…@example.com" in caplog.text
    assert "asha@" not in caplog.text and "the text" not in caplog.text  # no address, no trip


def sendgrid(answer: httpx.Response | Exception):
    """httpx with SendGrid answered by `answer`; what was asked is in the returned list."""
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    real = httpx.AsyncClient
    client = patch.object(
        notifications.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return client, asked


async def test_with_a_key_it_is_sent_by_sendgrid_from_the_verified_sender():
    client, asked = sendgrid(httpx.Response(202))
    with client, patch.multiple(settings, SENDGRID_API_KEY="SG.test-key", EMAIL_FROM="trips@tripplanner.example"):
        assert await notifications.send(EMAIL) == ("sent", "sendgrid", None)
    (request,) = asked
    assert str(request.url) == "https://api.sendgrid.com/v3/mail/send" and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer SG.test-key"
    assert json.loads(request.content) == {
        "personalizations": [{"to": [{"email": "asha@example.com"}]}],
        "from": {"email": "trips@tripplanner.example", "name": "TripPlanner"},
        "subject": "Your Goa itinerary is ready",
        "content": [{"type": "text/plain", "value": "the text"}, {"type": "text/html", "value": "<p>the html</p>"}],
    }


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(401, json={"errors": [{"message": "bad key"}]}), "SendGrid answered HTTP 401"),
        (httpx.Response(200), "SendGrid answered HTTP 200"),  # only 202 is "accepted for delivery"
        (httpx.ConnectError("refused"), "SendGrid could not be reached: ConnectError"),
    ],
)
async def test_what_sendgrid_did_not_take_is_failed_with_why(answer, error):
    client, _ = sendgrid(answer)
    with client, patch.multiple(settings, SENDGRID_API_KEY="SG.test-key", EMAIL_FROM="trips@tripplanner.example"):
        assert await notifications.send(EMAIL) == ("failed", "sendgrid", error)


async def test_a_key_with_no_sender_is_not_sent_at_all():
    client, asked = sendgrid(httpx.Response(202))
    with client, patch.multiple(settings, SENDGRID_API_KEY="SG.test-key", EMAIL_FROM=""):
        status, _, error = await notifications.send(EMAIL)
    assert status == "failed" and "EMAIL_FROM" in error and asked == []


# ── What each says ─────────────────────────────────────────────────────────


def test_the_ready_email_is_the_trips_summary_and_its_link():
    with patch.object(settings, "SITE_URL", "https://tripplanner.example/"):
        email = notifications.ready_email(USER, TRIP, PLAN)
    assert email.to == "asha@example.com" and email.subject == "Your Goa itinerary is ready"
    assert email.text.splitlines()[:5] == [
        "Your trip to Goa, 10 – 12 Jan 2030, is planned: 3 days, 3 places, ₹41,250 in all.",
        "",
        "Day 1: Fort Aguada, Baga Beach",  # free time is not a place
        "Day 2: Basilica of Bom Jesus",
        "Day 3: free time",
    ]
    link = f"https://tripplanner.example/trips/{TRIP.id}"
    assert f"See it, change it or download the PDF: {link}" in email.text
    assert f'<a href="{link}">' in email.html
    assert "https://tripplanner.example/settings" in email.text and "https://tripplanner.example/settings" in email.html


def test_what_the_plan_says_is_escaped_in_the_html():
    plan = {"total_cost": 0, "days": [{"day": 1, "morning": {"activity": '<img src=x onerror="alert(1)">'}}]}
    email = notifications.ready_email(USER, TRIP, plan)
    assert "<img" not in email.html and "&lt;img" in email.html


def test_the_reminder_is_day_ones_flight_stops_hotel_and_weather():
    leaves = datetime(2030, 1, 10, 6, 0, tzinfo=INDIA)
    email = notifications.reminder_email(USER, TRIP, PLAN, leaves, now=leaves - timedelta(hours=24))
    assert email.subject == "Your trip to Goa starts tomorrow"
    assert email.text.splitlines()[:7] == [
        "Your trip to Goa starts tomorrow at 06:00. Day 1:",
        "",
        "Flight 6E-204 leaves DEL at 06:00",
        "Weather: Rain, 29.0° / 24.0°",
        "13:00–15:00 Fort Aguada",
        "18:00–19:30 Baga Beach",
        "Staying at Goa Grand",
    ]
    assert f"/trips/{TRIP.id}" in email.text


@pytest.mark.parametrize(
    ("hours_before", "day"),
    [
        (48, "on Thursday 10 January"),
        (24, "tomorrow"),
        (4, "today"),  # planned the night before: sent late — and it does not say "tomorrow"
    ],
)
def test_the_reminder_says_the_day_as_seen_from_when_it_is_sent(hours_before, day):
    leaves = datetime(2030, 1, 10, 6, 0, tzinfo=INDIA)
    email = notifications.reminder_email(USER, TRIP, PLAN, leaves, now=leaves - timedelta(hours=hours_before))
    assert email.subject == f"Your trip to Goa starts {day}"
    assert email.text.startswith(f"Your trip to Goa starts {day} at 06:00.")


def test_a_reminder_of_a_plan_with_no_flight_or_weather_says_what_there_is():
    plan = {"days": [{"day": 1, "morning": {"activity": "Fort Aguada"}}]}
    leaves = datetime(2030, 1, 10, 8, 0, tzinfo=INDIA)
    email = notifications.reminder_email(USER, TRIP, plan, leaves, now=leaves - timedelta(hours=24))
    assert email.text.splitlines()[:3] == ["Your trip to Goa starts tomorrow at 08:00. Day 1:", "", "Fort Aguada"]


# ── When ───────────────────────────────────────────────────────────────────


def test_a_trip_begins_when_its_flight_leaves_on_its_first_day_in_indias_time():
    assert notifications.departure(TRIP, PLAN) == datetime(2030, 1, 10, 0, 30, tzinfo=timezone.utc)  # 06:00 IST


@pytest.mark.parametrize(
    "first_day",
    [
        {"day": 1},  # no flight
        {"day": 1, "flight": {"departure": "not a time"}},
        {"day": 1, "flight": {"departure": "2030-01-09T23:00:00"}},  # not the trip's first day
    ],
)
def test_without_a_flight_that_day_a_trip_begins_at_eight_in_the_morning(first_day):
    assert notifications.departure(TRIP, {"days": [first_day]}) == datetime(2030, 1, 10, 2, 30, tzinfo=timezone.utc)
    assert notifications.departure(TRIP, None) == datetime(2030, 1, 10, 2, 30, tzinfo=timezone.utc)


def test_a_departure_with_its_own_offset_keeps_it():
    plan = {"days": [{"flight": {"departure": "2030-01-10T06:00:00+04:00"}}]}  # Dubai's six o'clock
    assert notifications.departure(TRIP, plan) == datetime(2030, 1, 10, 2, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("hours", "before", "due"),
    [
        (24, timedelta(hours=24, seconds=1), False),  # not yet
        (24, timedelta(hours=24), True),  # on the hour
        (24, timedelta(hours=3), True),  # planned late: still reminded, before it leaves
        (24, timedelta(seconds=0), False),  # it has left
        (24, -timedelta(hours=1), False),
        (48, timedelta(hours=47), True),  # the traveller's own setting
        (48, timedelta(hours=49), False),
        (6, timedelta(hours=7), False),
    ],
)
def test_a_reminder_is_due_from_its_hours_before_until_the_trip_begins(hours, before, due):
    leaves = datetime(2030, 1, 10, 0, 30, tzinfo=timezone.utc)
    assert notifications.reminder_due(leaves, hours, leaves - before) is due


# ── Written down, and the traveller's "off" ────────────────────────────────


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


async def test_with_emails_off_nothing_is_sent_and_the_row_says_off():
    row = SimpleNamespace(status="sending", channel=None, error=None, sent_at=None, kind="trip_reminder", trip_id=1)
    compose, sent = MagicMock(), AsyncMock()
    with patch.object(notifications, "send", sent):
        assert await notifications._deliver(_db(), row, SimpleNamespace(notification_email=False), compose) == "off"
    compose.assert_not_called()
    sent.assert_not_called()
    assert (row.status, row.sent_at) == ("off", None)


@pytest.mark.parametrize("prefs", [None, SimpleNamespace(notification_email=True)])  # none saved: on, the default
async def test_with_emails_on_it_is_sent_and_the_row_says_how(prefs):
    row = SimpleNamespace(status="sending", channel=None, error=None, sent_at=None, kind="itinerary_ready", trip_id=1)
    with patch.object(notifications, "send", AsyncMock(return_value=("sent", "sendgrid", None))) as sent:
        assert await notifications._deliver(_db(), row, prefs, lambda: EMAIL) == "sent"
    sent.assert_awaited_once_with(EMAIL)
    assert (row.status, row.channel) == ("sent", "sendgrid") and row.sent_at is not None


async def test_a_failed_email_is_written_down_failed_and_logged(caplog):
    row = SimpleNamespace(status="sending", channel=None, error=None, sent_at=None, kind="itinerary_ready", trip_id=7)
    answer = ("failed", "sendgrid", "SendGrid answered HTTP 401")
    with patch.object(notifications, "send", AsyncMock(return_value=answer)):
        assert await notifications._deliver(_db(), row, None, lambda: EMAIL) == "failed"
    assert (row.error, row.sent_at) == ("SendGrid answered HTTP 401", None)
    assert "itinerary_ready for trip 7 failed: SendGrid answered HTTP 401" in caplog.text


async def test_the_ready_email_never_raises_into_the_run_that_spawned_it(caplog):
    with patch.object(notifications, "AsyncSessionLocal", MagicMock(side_effect=OSError("database away"))):
        assert await notifications.itinerary_ready(uuid.uuid4()) is None
    assert "could not be sent" in caplog.text


# ── Never in a request's way ───────────────────────────────────────────────


async def _planned(result: dict, has_itinerary: bool = False) -> tuple[AsyncMock, asyncio.Event]:
    """The background planning run, ended with `result`; the email it spawned waits until the event is set."""
    from app.api.routes.trips import _run_orchestrator

    let_go, started = asyncio.Event(), []

    async def slow_email(trip_id):
        started.append(trip_id)
        await let_go.wait()

    email = AsyncMock(side_effect=slow_email)
    trip_id = uuid.uuid4()
    with (
        patch.object(notifications, "itinerary_ready", email),
        patch("app.api.routes.trips.AsyncSessionLocal", MagicMock(return_value=AsyncMock())),
    ):
        await asyncio.wait_for(
            _run_orchestrator(trip_id, AsyncMock(), AsyncMock(return_value=result), has_itinerary=has_itinerary),
            timeout=5,
        )
        await asyncio.sleep(0)  # the spawned email starts
    email.trip_id = trip_id
    email.started = started
    return email, let_go


async def test_the_first_itinerary_spawns_its_email_and_the_run_does_not_wait_for_it():
    email, let_go = await _planned({"itinerary_id": str(uuid.uuid4())})
    assert email.started == [email.trip_id]  # it began, and the run had ended while it waited
    let_go.set()


@pytest.mark.parametrize(
    ("result", "has_itinerary"),
    [
        ({"itinerary_id": str(uuid.uuid4())}, True),  # a change to a plan: the traveller is on its page
        ({}, False),  # no plan: a question back, or nothing found
    ],
)
async def test_no_email_but_for_a_trips_first_itinerary(result, has_itinerary):
    email, _ = await _planned(result, has_itinerary)
    email.assert_not_called()


# ── The watcher ────────────────────────────────────────────────────────────


async def test_the_watcher_is_off_at_zero():
    checks = AsyncMock()
    with patch.object(notifications, "due_reminders", checks):
        await asyncio.wait_for(notifications.watch_reminders(0), timeout=1)
        with patch.object(settings, "REMINDER_CHECK_SECONDS", 0):
            await asyncio.wait_for(notifications.watch_reminders(), timeout=1)
    checks.assert_not_called()


async def test_the_watcher_keeps_looking_after_a_failed_look_and_stops_when_cancelled(caplog):
    looks = AsyncMock(side_effect=[RuntimeError("database away"), [], [], [], [], [], [], [], [], [], []])
    with patch.object(notifications, "due_reminders", looks):
        watching = asyncio.create_task(notifications.watch_reminders(0.001))

        async def three_looks():
            while looks.await_count < 3 and not watching.done():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(three_looks(), timeout=2)  # a watcher that stopped fails here, not by hanging
        assert not watching.done() and looks.await_count >= 3  # it went on after the failed one
        watching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watching
    assert "Looking for due reminders failed" in caplog.text


# ── The settings ───────────────────────────────────────────────────────────

BODY = {"dietary_restrictions": [], "preferred_airlines": [], "travel_style": None, "home_city": None}


@pytest.mark.parametrize("hours", [0, 169, -24])
async def test_a_reminder_is_between_an_hour_and_a_week_before(hours):
    resp = await _call("PUT", _route_session(), BODY | {"reminder_hours_before": hours})
    assert resp.status_code == 422


async def test_the_two_settings_are_saved_as_given():
    session = _route_session()
    resp = await _call("PUT", session, BODY | {"notification_email": False, "reminder_hours_before": 168})
    assert resp.status_code == 200
    saved = session.add.call_args.args[0]
    assert (saved.notification_email, saved.reminder_hours_before) == (False, 168)
    assert (resp.json()["notification_email"], resp.json()["reminder_hours_before"]) == (False, 168)


async def test_a_put_without_them_leaves_the_defaults():
    session = _route_session()
    resp = await _call("PUT", session, BODY)
    assert (resp.json()["notification_email"], resp.json()["reminder_hours_before"]) == (True, 24)


# ── The pre-start check ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"SENDGRID_API_KEY": ""}, "SENDGRID_API_KEY is empty: no email is sent"),
        ({"EMAIL_FROM": ""}, "EMAIL_FROM is empty: SendGrid sends only from a verified sender"),
        ({"SITE_URL": "http://localhost:3000"}, "SITE_URL points at this machine"),
    ],
)
def test_a_deployment_that_cannot_email_is_told_and_still_starts(change, says):
    report = preflight.check(deployment(**change), providers())
    assert report.ok and [w for w in report.warnings if says in w]
    assert "SG." not in report.text()  # a key is named, never printed


def test_no_sender_is_only_said_when_there_is_a_key_to_send_with():
    report = preflight.check(deployment(SENDGRID_API_KEY="", EMAIL_FROM=""), providers())
    assert [w.split(" ")[0] for w in report.warnings] == ["SENDGRID_API_KEY"]
