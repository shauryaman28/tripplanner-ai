"""A plan's booking links, as a traveller follows them (Phase 39).

    python scripts/booking_check.py --prodlike           # a trip planned on the production-like stack, its links opened
    python scripts/booking_check.py --api http://localhost:8000 --no-open

One trip planned — Goa, forty days out, three days, two travellers — and its itinerary read back. The flight's
link must be a Google Flights search for its route, its days and its travellers; the hotel's a Booking.com search
for its name and town, its nights and its guests. Then each is opened in Chromium (scripts/booking_open.js, with
the frontend's Playwright) and what the page filled in is read: the route and the dates, the destination, the
dates, the guests, and the first hotel found. Opening them asks Google and Booking.com one page each.
Exit code 0 when every check held.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent


class Checks:
    def __init__(self) -> None:
        self.passed = self.failed = 0

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        self.passed, self.failed = self.passed + bool(ok), self.failed + (not ok)
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  — {detail}" if detail else ""), flush=True)
        return bool(ok)


def q(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="A plan's booking links, followed.")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--prodlike", action="store_true")
    parser.add_argument("--no-open", action="store_true", help="check the links, do not open them")
    args = parser.parse_args()
    api = "http://localhost:8200" if args.prodlike else args.api.rstrip("/")
    checks = Checks()
    start = date.today() + timedelta(days=40)
    end = start + timedelta(days=2)
    with httpx.Client(base_url=api, timeout=60) as http:
        creds = {"email": f"booking-check-{secrets.token_hex(4)}@example.com", "password": secrets.token_urlsafe(14)}
        assert http.post("/auth/register", json=creds).status_code == 201, "could not make an account"
        login = http.post("/auth/login", data={"username": creds["email"], "password": creds["password"]})
        http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        body = {"destination": "Goa", "start_date": str(start), "end_date": str(end), "budget": 80_000,
                "group_size": 2, "interests": ["history", "beaches"]}  # fmt: skip
        trip_id = http.post("/trips", json=body).json()["id"]
        began = time.perf_counter()
        assert http.post(f"/trips/{trip_id}/plan", json={"raw_input": "A long weekend"}).status_code == 202
        for _ in range(300):
            if any(r["agent_name"] == "orchestrator" for r in http.get(f"/trips/{trip_id}/runs").json()):
                break
            time.sleep(1)
        print(f"{api}\nA plan for Goa, {start} to {end}, two travellers — {time.perf_counter() - began:.1f} s")
        saved = http.get(f"/trips/{trip_id}/itinerary")
        if not checks.expect(saved.status_code == 200, "a plan was made", f"HTTP {saved.status_code}"):
            return 1
        day = saved.json()["structured_data"]["days"][0]

    flight, hotel = day.get("flight") or {}, day.get("hotel") or {}
    route = f"{(flight.get('origin') or {}).get('code')} → {(flight.get('destination') or {}).get('code')}"
    wanted_q = f"Flights to {(flight.get('destination') or {}).get('code')} from {(flight.get('origin') or {}).get('code')} on {start} through {end} for 2 adults"
    links = []
    if checks.expect(bool(flight.get("booking_url")), f"the flight ({route}) has its link"):
        checks.expect(q(flight["booking_url"]).get("q") == wanted_q, "…a Google Flights search for its route, days and travellers",
                      q(flight["booking_url"]).get("q", ""))  # fmt: skip
        links.append(flight["booking_url"])
    if checks.expect(bool(hotel.get("booking_url")), f"the hotel ({hotel.get('name')}) has its link"):
        got = q(hotel["booking_url"])
        checks.expect(
            (got.get("ss"), got.get("checkin"), got.get("checkout"), got.get("group_adults")) == (f"{hotel.get('name')}, Goa", str(start), str(end), "2"),
            "…a Booking.com search for it, its nights and its guests", f"{got.get('ss')} · {got.get('checkin')} to {got.get('checkout')}",
        )  # fmt: skip
        links.append(hotel["booking_url"])
    if args.no_open or not links:
        print(f"\n{checks.passed} passed, {checks.failed} failed")
        return 0 if not checks.failed else 1

    print("\nOpened in Chromium:")
    opened = subprocess.run(
        ["node", str(ROOT / "scripts" / "booking_open.js"), *links],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "NODE_PATH": str(ROOT / "src" / "frontend" / "node_modules")},
    )  # fmt: skip
    pages = [json.loads(line) for line in opened.stdout.splitlines() if line.startswith("{")]
    for seen in pages:
        print(f"     {seen.get('title')}")
        if "google.com" in seen["url"]:
            fields = " ".join(seen.get("fields") or [])
            checks.expect(
                "Departure=" in fields and "Return=" in fields and str(seen.get("trip", "")).startswith("Round trip 2"),
                "Google Flights opens on the route, both days and two travellers",
                f"{seen.get('trip')} · {', '.join(f for f in seen.get('fields') or [] if f.startswith(('Where', 'Departure', 'Return')))[:200]}",
            )
        else:
            checks.expect(
                bool(seen.get("dates")) and str(seen.get("occupancy", "")).startswith("2 adults"),
                "Booking.com opens on the nights and the guests",
                f"{seen.get('dates')} · {seen.get('occupancy')} · first: {seen.get('first')}",
            )
            checks.expect(
                seen.get("first") == hotel.get("name"), "…with the plan's hotel first", f"{seen.get('first')}"
            )
    checks.expect(
        len(pages) == len(links) and not any("error" in p for p in pages), "every link opened", opened.stderr[-200:]
    )
    print(f"\n{checks.passed} passed, {checks.failed} failed")
    return 0 if not checks.failed else 1


if __name__ == "__main__":
    sys.exit(main())
