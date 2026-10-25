"""Phase 40 — the two emails a traveller is sent: "Your itinerary is ready", and "Your trip starts tomorrow".

itinerary_ready(trip_id)    when a trip's first itinerary has been saved: a summary — the days, the places, the
                            total — and the link to it. Spawned by the planning run once it is over: nothing
                            waits for it, and its failure is only logged.
due_reminders(now)          every trip whose reminder is due: `reminder_hours_before` (24 unless changed) before
                            it begins — before its flight leaves, or 08:00 on its first day when it has none —
                            and not yet past. Day 1's plan: the flight, each stop's time, the hotel, the weather.
                            `now` is a parameter, as Phase 3's cache expiry was tested: the tests set the clock.
watch_reminders()           looks for due reminders every REMINDER_CHECK_SECONDS, for as long as the backend runs.

Every email is first written down (`notifications`, unique by trip, kind and what it is about), then sent: a
second backend looking for the same reminder finds the row and leaves it. With `notification_email` off the row
says "off" and nothing is sent. Sent by SendGrid when SENDGRID_API_KEY is set; otherwise written to the log —
whole in development, and in production only that it was not sent (an email address and a trip are not for a
log there).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
from html import escape
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.itinerary import Itinerary
from app.models.notification import Notification
from app.models.trip import Trip, TripStatus
from app.models.user import User
from app.models.user_preferences import UserPreferences

logger = logging.getLogger(__name__)

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
INDIA = ZoneInfo("Asia/Kolkata")  # every trip the planner makes is in India, and its times are India's
DAY_STARTS = time(8, 0)  # a trip with no flight begins at 08:00 on its first day (Phase 37's day)
SLOTS = ("morning", "afternoon", "evening")
FREE_TIME = "Explore the area"


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str


def _production() -> bool:
    return str(settings.APP_ENV).lower() == "production"


def _masked(address: str) -> str:
    name, _, domain = address.partition("@")
    return f"{name[:1]}…@{domain}" if domain else "…"


async def send(email: Email) -> tuple[str, str, str | None]:
    """Send one email. Returns (status, channel, error): ("sent", "sendgrid", None), or ("logged", "log", None)."""
    if not settings.SENDGRID_API_KEY:
        if _production():
            logger.warning("[EMAIL] not sent — no SENDGRID_API_KEY: %r to %s", email.subject, _masked(email.to))
        else:
            logger.info("[EMAIL] to %s — %s\n%s", email.to, email.subject, email.text)
        return "logged", "log", None
    if not settings.EMAIL_FROM:
        return "failed", "sendgrid", "EMAIL_FROM is not set: SendGrid sends only from a verified sender"
    body = {
        "personalizations": [{"to": [{"email": email.to}]}],
        "from": {"email": settings.EMAIL_FROM, "name": "TripPlanner"},
        "subject": email.subject,
        "content": [{"type": "text/plain", "value": email.text}, {"type": "text/html", "value": email.html}],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            answer = await client.post(
                SENDGRID_URL, json=body, headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"}
            )
    except httpx.HTTPError as exc:
        return "failed", "sendgrid", f"SendGrid could not be reached: {type(exc).__name__}"
    if answer.status_code == 202:
        return "sent", "sendgrid", None
    return "failed", "sendgrid", f"SendGrid answered HTTP {answer.status_code}"


# ── What each email says ───────────────────────────────────────────────────


def _days(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return f"{start.day} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"


def _stops(day: dict) -> list[dict]:
    return [day[slot] for slot in SLOTS if isinstance(day.get(slot), dict) and day[slot].get("activity") != FREE_TIME]


def _rupees(amount: object) -> str:
    return f"₹{float(amount):,.0f}" if isinstance(amount, (int, float)) else "—"


def _link(trip: Trip) -> str:
    return f"{settings.SITE_URL.rstrip('/')}/trips/{trip.id}"


def _footer() -> tuple[str, str]:
    where = f"{settings.SITE_URL.rstrip('/')}/settings"
    text = f"You are sent this because email notifications are on. Turn them off or change the reminder: {where}"
    return (
        text,
        f'<p style="color:#666;font-size:12px">{escape(text.split(":")[0])}: <a href="{where}">settings</a></p>',
    )


def ready_email(user: User, trip: Trip, plan: dict) -> Email:
    """'Your itinerary is ready': the trip's dates, each day's places, the total, the link."""
    days = plan.get("days") or []
    lines = [f"Day {day.get('day')}: " + (", ".join(s["activity"] for s in _stops(day)) or "free time") for day in days]
    places = sum(len(_stops(day)) for day in days)
    summary = (
        f"Your trip to {trip.destination}, {_days(trip.start_date, trip.end_date)}, is planned: {len(days)} days, "
        f"{places} places, {_rupees(plan.get('total_cost'))} in all."
    )
    note, note_html = _footer()
    text = "\n".join([summary, "", *lines, "", f"See it, change it or download the PDF: {_link(trip)}", "", note])
    html = (
        f"<p>{escape(summary)}</p><ul>{''.join(f'<li>{escape(line)}</li>' for line in lines)}</ul>"
        f'<p><a href="{escape(_link(trip))}">See your itinerary</a></p>{note_html}'
    )
    return Email(user.email, f"Your {trip.destination} itinerary is ready", text, html)


def reminder_email(user: User, trip: Trip, plan: dict, leaves: datetime, hours: int) -> Email:
    """'Your trip starts tomorrow': day 1 — the flight, each stop and its time, the hotel, the weather."""
    days = plan.get("days") or []
    first = days[0] if days else {}
    when = "tomorrow" if hours == 24 else f"on {leaves:%A %-d %B} at {leaves:%H:%M}"
    lines = []
    flight = first.get("flight") or {}
    if flight.get("departure"):
        lines.append(
            f"Flight {flight.get('flight_number') or flight.get('airline') or ''}".strip()
            + f" leaves {(flight.get('origin') or {}).get('code', '')} at {str(flight['departure'])[11:16]}"
        )
    weather = first.get("weather") or {}
    if weather.get("condition"):
        lines.append(
            f"Weather: {weather['condition']}, {weather.get('temp_high_c', '?')}° / {weather.get('temp_low_c', '?')}°"
        )
    for stop in _stops(first):
        times = f"{stop['start_time']}–{stop['end_time']} " if stop.get("start_time") else ""
        lines.append(f"{times}{stop['activity']}")
    if (first.get("hotel") or {}).get("name"):
        lines.append(f"Staying at {first['hotel']['name']}")
    note, note_html = _footer()
    intro = f"Your trip to {trip.destination} starts {when}. Day 1:"
    text = "\n".join([intro, "", *lines, "", f"The whole plan: {_link(trip)}", "", note])
    html = (
        f"<p>{escape(intro)}</p><ul>{''.join(f'<li>{escape(line)}</li>' for line in lines)}</ul>"
        f'<p><a href="{escape(_link(trip))}">The whole plan</a></p>{note_html}'
    )
    return Email(user.email, f"Your trip to {trip.destination} starts {when}", text, html)


# ── Written down once, then sent ───────────────────────────────────────────


async def _claim(db: AsyncSession, trip: Trip, kind: str, about: str) -> Notification | None:
    """The row that says this email is being sent — or None when it was already (by this backend or another)."""
    row = Notification(user_id=trip.user_id, trip_id=trip.id, kind=kind, about=about, status="sending")
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    return row


async def _deliver(
    db: AsyncSession, row: Notification, prefs: UserPreferences | None, compose: Callable[[], Email]
) -> str:
    if prefs is not None and not prefs.notification_email:
        row.status = "off"
    else:
        row.status, row.channel, row.error = await send(compose())
        row.sent_at = datetime.now(timezone.utc).replace(tzinfo=None) if row.status in ("sent", "logged") else None
    db.add(row)
    await db.commit()
    if row.status == "failed":
        logger.warning("[EMAIL] %s for trip %s failed: %s", row.kind, row.trip_id, row.error)
    return row.status


async def _latest_plan(db: AsyncSession, trip_id: uuid.UUID) -> dict | None:
    found = await db.execute(
        select(Itinerary.structured_data)
        .where(Itinerary.trip_id == trip_id)
        .order_by(Itinerary.created_at.desc())
        .limit(1)
    )
    plan = found.scalar_one_or_none()
    return plan if isinstance(plan, dict) else None


async def itinerary_ready(trip_id: uuid.UUID) -> str | None:
    """'Your itinerary is ready' for a trip's first itinerary — once. Never raises (it is spawned)."""
    try:
        async with AsyncSessionLocal() as db:
            trip = await db.get(Trip, trip_id)
            plan = await _latest_plan(db, trip_id) if trip else None
            if trip is None or plan is None:
                return None
            row = await _claim(db, trip, "itinerary_ready", "first")
            if row is None:
                return None  # a later itinerary of the same trip — a change: the traveller is on the page
            user, prefs = await db.get(User, trip.user_id), await db.get(UserPreferences, trip.user_id)
            return await _deliver(db, row, prefs, partial(ready_email, user, trip, plan))
    except Exception:
        logger.exception("The itinerary-ready email for trip %s could not be sent", trip_id)
        return None


def departure(trip: Trip, plan: dict | None) -> datetime:
    """When the trip begins, in UTC: its flight's departure on its first day, or 08:00 that day."""
    first = ((plan or {}).get("days") or [{}])[0]
    leaves = str((first.get("flight") or {}).get("departure") or "")
    try:
        at = datetime.fromisoformat(leaves)
        if at.date() != trip.start_date:
            raise ValueError("not the first day")
    except ValueError:
        at = datetime.combine(trip.start_date, DAY_STARTS)
    return (at if at.tzinfo else at.replace(tzinfo=INDIA)).astimezone(timezone.utc)


def reminder_due(leaves: datetime, hours: int, now: datetime) -> bool:
    """From `hours` before the trip begins until it has begun — a trip planned late still gets its reminder."""
    return leaves - timedelta(hours=hours) <= now < leaves


async def due_reminders(now: datetime | None = None) -> list[tuple[uuid.UUID, str]]:
    """Send every reminder that is due at `now` (UTC). Returns (trip id, what became of its email) for each sent."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(INDIA).date()
    done: list[tuple[uuid.UUID, str]] = []
    async with AsyncSessionLocal() as db:
        trips = (
            (
                await db.execute(
                    select(Trip).where(
                        Trip.status == TripStatus.COMPLETED,
                        Trip.start_date >= today,
                        Trip.start_date <= today + timedelta(days=8),  # the reminder is at most 168 hours before
                    )
                )
            )
            .scalars()
            .all()
        )
        for trip in trips:
            try:
                prefs = await db.get(UserPreferences, trip.user_id)
                hours = prefs.reminder_hours_before if prefs else 24
                plan = await _latest_plan(db, trip.id)
                leaves = departure(trip, plan)
                if plan is None or not reminder_due(leaves, hours, now):
                    continue
                row = await _claim(db, trip, "trip_reminder", trip.start_date.isoformat())
                if row is None:
                    continue
                user = await db.get(User, trip.user_id)
                local = leaves.astimezone(INDIA)
                compose = partial(reminder_email, user, trip, plan, local, hours)
                done.append((trip.id, await _deliver(db, row, prefs, compose)))
            except Exception:
                logger.exception("The reminder for trip %s could not be sent", trip.id)
                await db.rollback()
    return done


async def watch_reminders(every: float | None = None) -> None:
    """For as long as the backend runs: send the reminders that are due, every REMINDER_CHECK_SECONDS (0: never)."""
    every = settings.REMINDER_CHECK_SECONDS if every is None else every
    if every <= 0:
        return
    while True:
        try:
            await due_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Looking for due reminders failed — trying again later")
        await asyncio.sleep(every)
