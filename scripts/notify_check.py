"""The two emails, on a running stack (Phase 40).

    PRODLIKE_REMINDER_CHECK_SECONDS=15 python scripts/prodlike.py up
    python scripts/notify_check.py --prodlike                       # without SendGrid: each email logged as not sent
    python scripts/notify_check.py --prodlike --to you@gmail.com     # with SENDGRID_API_KEY and EMAIL_FROM in .env:
                                                                       sent to you+notify-…@gmail.com

Two travellers, two trips to Goa that begin tomorrow — real plans, a minute apart (Groq's free tier). The first
has emails on and the reminder 48 hours before: its "itinerary is ready" email is written down when its plan is
saved, and the backend's own watcher sends its reminder within one of its rounds — the trip begins within 48 hours.
The second has emails off: both its emails are written down "off", and nothing is sent or logged for it. What
became of each email is read from the stack's database; what the backend said, from its log — which must never
hold a traveller's address.
Exit code 0 when every check held.
"""

from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

DATABASE = ["docker", "exec", "tripplanner-prodlike-neon-standin-1", "psql", "-U", "tripplanner", "-d", "tripplanner"]
BACKEND = "tripplanner-prodlike-backend-1"
INDIA = ZoneInfo("Asia/Kolkata")


class Checks:
    def __init__(self) -> None:
        self.passed = self.failed = 0

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        self.passed, self.failed = self.passed + bool(ok), self.failed + (not ok)
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  — {detail}" if detail else ""), flush=True)
        return bool(ok)


def rows(trip_id: str) -> list[tuple[str, ...]]:
    sql = (
        "SELECT kind, about, status, coalesce(channel, ''), coalesce(error, '') FROM notifications "
        f"WHERE trip_id = '{trip_id}' ORDER BY created_at"
    )
    out = subprocess.run([*DATABASE, "-At", "-F", "|", "-c", sql], capture_output=True, text=True, check=True)
    return [tuple(line.split("|")) for line in out.stdout.splitlines() if line]


def backend_log(since: str) -> str:
    out = subprocess.run(["docker", "logs", "--since", since, BACKEND], capture_output=True, text=True)
    return out.stdout + out.stderr


def traveller(http: httpx.Client, email: str, **settings) -> None:
    creds = {"email": email, "password": secrets.token_urlsafe(14)}
    assert http.post("/auth/register", json=creds).status_code == 201, f"could not make an account for {email}"
    login = http.post("/auth/login", data={"username": creds["email"], "password": creds["password"]})
    http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    prefs = http.get("/users/preferences").json()
    prefs.pop("updated_at", None)
    saved = http.put("/users/preferences", json=prefs | settings)
    assert saved.status_code == 200, saved.text


def plan(http: httpx.Client, start: date) -> tuple[str, dict, float]:
    body = {"destination": "Goa", "start_date": str(start), "end_date": str(start + timedelta(days=2)),
            "budget": 80_000, "group_size": 2, "interests": ["history", "beaches"]}  # fmt: skip
    trip_id = http.post("/trips", json=body).json()["id"]
    began = time.perf_counter()
    assert http.post(f"/trips/{trip_id}/plan", json={"raw_input": "A long weekend"}).status_code == 202
    for _ in range(300):
        if any(r["agent_name"] == "orchestrator" for r in http.get(f"/trips/{trip_id}/runs").json()):
            break
        time.sleep(1)
    saved = http.get(f"/trips/{trip_id}/itinerary")
    return (
        trip_id,
        (saved.json().get("structured_data") or {}) if saved.status_code == 200 else {},
        time.perf_counter() - began,
    )


def until(condition, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if found := condition():
            return found
        time.sleep(2)
    return condition()


def main() -> int:
    parser = argparse.ArgumentParser(description="The two emails, on a running stack.")
    parser.add_argument("--api", default="http://localhost:8200")
    parser.add_argument("--prodlike", action="store_true")
    parser.add_argument("--to", help="your address: the first traveller's emails go to it, +notify-… added")
    parser.add_argument("--wait", type=float, default=330, help="seconds to wait for the watcher's reminder")
    args = parser.parse_args()
    api = "http://localhost:8200" if args.prodlike else args.api.rstrip("/")
    checks = Checks()
    tag = secrets.token_hex(3)
    if args.to:
        name, _, domain = args.to.partition("@")
        on_address = f"{name}+notify-{tag}@{domain}"
    else:
        on_address = f"notify-on-{tag}@example.com"
    off_address = f"quiet-{tag}@example.com"
    since = datetime.now().astimezone().isoformat(timespec="seconds")
    start = datetime.now(INDIA).date() + timedelta(days=1)  # tomorrow, in India: within any 48 hours of its flight

    with httpx.Client(base_url=api, timeout=60) as http:
        traveller(http, on_address, notification_email=True, reminder_hours_before=48)
        on_trip, on_plan, took = plan(http, start)
    print(f"{api}\nEmails on, reminder 48 hours before — Goa from {start}: planned in {took:.1f} s")
    if not checks.expect(bool(on_plan.get("days")), "a plan was made"):
        return 1
    ready = until(lambda: [r for r in rows(on_trip) if r[0] == "itinerary_ready" and r[2] != "sending"], 30)
    checks.expect(bool(ready), "its 'itinerary is ready' email is written down", " · ".join("|".join(r) for r in ready))
    sent_or_logged = ready and ready[0][2] in ("sent", "logged")
    checks.expect(sent_or_logged, "…sent, or logged where there is no SendGrid key", ready[0][2] if ready else "")
    reminded = until(lambda: [r for r in rows(on_trip) if r[0] == "trip_reminder"], args.wait)
    checks.expect(
        bool(reminded) and reminded[0][1] == str(start) and reminded[0][2] in ("sent", "logged"),
        "the watcher sent its reminder, for the day it begins",
        " · ".join("|".join(r) for r in reminded) or f"nothing in {args.wait:.0f} s",
    )

    print("\nA minute for Groq's free tier, then a traveller with emails off", flush=True)
    time.sleep(60)
    with httpx.Client(base_url=api, timeout=60) as http:
        traveller(http, off_address, notification_email=False)
        off_trip, off_plan, took = plan(http, start)
    print(f"Emails off — Goa from {start}: planned in {took:.1f} s")
    checks.expect(bool(off_plan.get("days")), "a plan was made")
    quiet = until(lambda: len(rows(off_trip)) >= 2 and rows(off_trip), args.wait) or []
    checks.expect(
        [(r[0], r[2]) for r in quiet] == [("itinerary_ready", "off"), ("trip_reminder", "off")],
        "both its emails are written down off",
        " · ".join("|".join(r) for r in quiet),
    )

    log = backend_log(since)
    said = [line for line in log.splitlines() if "[EMAIL]" in line]
    print("\nThe backend's log:")
    for line in said:
        print(f"     {line[:220]}")
    on_masked, off_masked = f"{on_address[0]}…@{on_address.split('@')[1]}", f"{off_address[0]}…@example.com"
    if ready and ready[0][3] == "log":
        checks.expect(
            sum("Your Goa itinerary is ready" in line and on_masked in line for line in said) == 1
            and sum("Your trip to Goa starts" in line and on_masked in line for line in said) == 1,
            "each of the first traveller's emails is said to be not sent — no SENDGRID_API_KEY",
        )
    else:
        checks.expect(not said, "nothing logged: SendGrid sent them", f"to {on_address}")
    checks.expect(not any(off_masked in line for line in said), "nothing for the traveller with emails off")
    checks.expect(on_address not in log and off_address not in log, "no traveller's address in the log")
    print(f"\n{checks.passed} passed, {checks.failed} failed")
    return 0 if not checks.failed else 1


if __name__ == "__main__":
    sys.exit(main())
