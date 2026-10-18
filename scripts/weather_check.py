"""A plan's weather, as a traveller finds it (Phase 38).

    python scripts/weather_check.py --prodlike                   # Goa, tomorrow, a week — on the production-like stack
    python scripts/weather_check.py --prodlike --destination Mumbai --days 7
    python scripts/weather_check.py --api http://localhost:8000

One trip planned from tomorrow, so that OpenWeatherMap's real forecast reaches its first days and the climate
estimate the rest, and its itinerary read back. For each day: the weather, and each stop indoor or outdoor.
The checks:
  - every day has its weather — or none, only for a place the climate table does not know, past the forecast
  - the forecast's days come first, the estimate's after; an estimate says so
  - a rainy day has an indoor stop, or the note that says none could be given
  - the `weather` step logged what it found

A plan asks the models and providers once each, and OpenWeatherMap once (the warmer asked it first, so the
plan's own ask is a cache hit). Exit code 0 when every check held.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src" / "backend")]

from app.pdf.plan import weather_words  # noqa: E402 — the PDF's words for a day's weather, which are the page's
from src.ai import weather  # noqa: E402
from src.ai.itinerary import FREE_TIME, SLOTS  # noqa: E402


class Checks:
    def __init__(self) -> None:
        self.passed = self.failed = 0

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        self.passed, self.failed = self.passed + bool(ok), self.failed + (not ok)
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  — {detail}" if detail else ""), flush=True)
        return bool(ok)


def main() -> int:
    parser = argparse.ArgumentParser(description="A plan's weather, as a traveller finds it.")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--prodlike", action="store_true", help="the production-like stack")
    parser.add_argument("--destination", default="Goa")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    api = "http://localhost:8200" if args.prodlike else args.api.rstrip("/")
    checks = Checks()
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=args.days - 1)
    with httpx.Client(base_url=api, timeout=60) as http:
        creds = {"email": f"weather-check-{secrets.token_hex(4)}@example.com", "password": secrets.token_urlsafe(14)}
        assert http.post("/auth/register", json=creds).status_code == 201, "could not make an account"
        login = http.post("/auth/login", data={"username": creds["email"], "password": creds["password"]})
        http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        body = {"destination": args.destination, "start_date": str(start), "end_date": str(end), "budget": 90_000,
                "group_size": 1, "interests": ["history", "beaches", "museums", "food"]}  # fmt: skip
        trip_id = http.post("/trips", json=body).json()["id"]
        began = time.perf_counter()
        assert http.post(f"/trips/{trip_id}/plan", json={"raw_input": "A relaxed week"}).status_code == 202
        for _ in range(300):
            runs = http.get(f"/trips/{trip_id}/runs").json()
            if any(run["agent_name"] == "orchestrator" for run in runs):
                break
            time.sleep(1)
        print(f"{api}\nA plan for {args.destination}, {start} to {end} — {time.perf_counter() - began:.1f} s")
        saved = http.get(f"/trips/{trip_id}/itinerary")
        if not checks.expect(saved.status_code == 200, "a plan was made", f"HTTP {saved.status_code}"):
            return 1
        plan = saved.json()["structured_data"]
        attractions = (
            next((r["output"].get("attractions") for r in runs if r["agent_name"] == "activities_agent"), []) or []
        )

    category = {a.get("name"): a.get("category") for a in attractions}
    missing, order, rainy_bad = [], [], []
    for day in plan["days"]:
        said = day.get("weather")
        words = weather_words(said) or "—"
        tag = " (climate estimate)" if said and said.get("estimate") else ""
        print(
            f"     Day {day['day']} {day['date']}  {words}{tag}"
            + (f"  — {day['weather_note']}" if day.get("weather_note") else "")
        )
        stops = [day[slot] for slot in SLOTS if day.get(slot)]
        for stop in stops:
            if stop["activity"] != FREE_TIME:
                print(
                    f"         {weather.setting(stop.get('category') or category.get(stop['activity'])):7} {stop['activity']}"
                )
        if not said:
            missing.append(day["day"])
            continue
        order.append(said.get("estimate", False))
        indoors = [s for s in stops if weather.setting(s.get("category") or category.get(s["activity"])) == "indoor"]
        if said.get("kind") == "rainy" and not indoors and not day.get("weather_note"):
            rainy_bad.append(day["day"])
    checks.expect(not missing, "every day has its weather", f"none on day {missing}" if missing else "")
    checks.expect(order == sorted(order), "the forecast's days first, the estimate's after", f"{order}")
    rainy = [d["day"] for d in plan["days"] if (d.get("weather") or {}).get("kind") == "rainy"]
    checks.expect(
        not rainy_bad, "every rainy day has an indoor stop, or says why not", f"rainy days: {rainy or 'none'}"
    )
    read = next((r for r in runs if r["agent_name"] == "weather"), None)
    checks.expect(
        read is not None and read["status"] == "completed",
        "the weather step logged what it found",
        f"{read['output']}" if read else "no row",
    )
    print(f"\n{checks.passed} passed, {checks.failed} failed")
    return 0 if not checks.failed else 1


if __name__ == "__main__":
    sys.exit(main())
