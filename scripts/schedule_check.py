"""A plan's clock and the travel-time tool, as a traveller and an operator find them (Phase 37).

    python scripts/schedule_check.py --route       # Fort Aguada → Baga Beach on Google's Routes API, twice
    python scripts/schedule_check.py --prodlike    # a plan on the production-like stack: every stop's clock
    python scripts/schedule_check.py --api http://localhost:8000

--route  The roadmap's Dev B acceptance: the tool, run here with .env's GOOGLE_MAPS_API_KEY and REDIS_URL, asked
         the same journey twice — the first answer Google's (`[CACHE MISS]`), the second from the cache
         (`[CACHE HIT]`), and one request to Google between them. Without a key it says so.
--api    Dev A's: one trip planned — Goa, three days, forty days out — and its itinerary read back. Every stop
         has a start and an end; every stop more than 10 km from where the traveller was has a travel note;
         nothing before 08:00 or after 22:00, no two stops at once. And how many of the ways Google answered:
         every one with a key, none without (the rest are estimates, and say so).

A plan asks the models and the providers once each; the route asks Google one element (of 10,000 free a
month). Exit code 0 when every check held.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src" / "backend")]

from app.pdf.plan import travel_words  # noqa: E402 — the PDF's words for a journey, which are the page's
from src.ai import schedule  # noqa: E402
from src.ai.itinerary import SLOTS  # noqa: E402

FORT_AGUADA, BAGA_BEACH = (15.4925, 73.7735), (15.5553, 73.7517)


class Checks:
    def __init__(self) -> None:
        self.passed = self.failed = 0

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        self.passed, self.failed = self.passed + bool(ok), self.failed + (not ok)
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  — {detail}" if detail else ""), flush=True)
        return bool(ok)


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def the_route(checks: Checks) -> None:
    from src.ai.mcp_server import tools
    from src.ai.mcp_server.cache import _get_client, make_cache_key
    from src.ai.mcp_server.models import ToolError, TravelTimeInput

    print("Fort Aguada → Baga Beach, by auto-rickshaw (asked of Google as a car)")
    if not checks.expect(bool(tools.mcp_settings.GOOGLE_MAPS_API_KEY), "GOOGLE_MAPS_API_KEY is set (.env)"):
        print("     put a key of a project with the Routes API in .env — HOW_TO_RUN.md, Step 35")
        return
    asked = TravelTimeInput(origin_lat=FORT_AGUADA[0], origin_lng=FORT_AGUADA[1], dest_lat=BAGA_BEACH[0],
                            dest_lng=BAGA_BEACH[1], mode="auto-rickshaw")  # fmt: skip
    key = make_cache_key(
        "travel", {**{k: round(v, 5) for k, v in asked.model_dump().items() if k != "mode"}, "mode": asked.mode}
    )
    redis = _get_client()
    if redis is not None:
        redis.delete(key)  # so that the first answer is Google's, not one cached by an earlier run
    lines = _Lines()
    logging.getLogger("src.ai.mcp_server.cache").addHandler(lines)
    logging.getLogger("src.ai.mcp_server.cache").setLevel(logging.INFO)
    sent: list[str] = []
    real_post = tools.httpx.post

    def counted(url: str, **request):
        sent.append(url)
        return real_post(url, **request)

    tools.httpx.post = counted
    try:
        began = time.perf_counter()
        first = tools.get_travel_time(asked)
        took = (time.perf_counter() - began) * 1000
        second = tools.get_travel_time(asked)
    finally:
        tools.httpx.post = real_post
    if not checks.expect(
        not isinstance(first, ToolError),
        "Google answered",
        f"{first}" if isinstance(first, ToolError) else f"{took:.0f} ms",
    ):
        return
    straight = schedule.straight_km(FORT_AGUADA, BAGA_BEACH)
    print(
        f"     {first.minutes} min, {first.km} km by road ({straight:.1f} km as the crow flies) — Google, without traffic"
    )
    checks.expect(5 <= first.minutes <= 45 and 8 <= first.km <= 16, "a time and a distance a drive of 7 km can have")
    checks.expect(second == first, "the second answer is the first")
    checks.expect(len(sent) == 1, "Google was asked once", f"{len(sent)} requests")
    checks.expect(f"[CACHE MISS] {key}" in lines.lines and f"[CACHE HIT]  {key}" in lines.lines,
                  "[CACHE MISS], then [CACHE HIT], in the log", key)  # fmt: skip
    if redis is not None:
        checks.expect(86_000 < redis.ttl(key) <= 86_400, "kept for a day", f"TTL {redis.ttl(key)} s")


def minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def a_plan(checks: Checks, api: str) -> None:
    start = date.today() + timedelta(days=40)
    with httpx.Client(base_url=api, timeout=60) as http:
        creds = {"email": f"schedule-check-{secrets.token_hex(4)}@example.com", "password": secrets.token_urlsafe(14)}
        assert http.post("/auth/register", json=creds).status_code == 201, "could not make an account"
        login = http.post("/auth/login", data={"username": creds["email"], "password": creds["password"]})
        http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        body = {"destination": "Goa", "start_date": str(start), "end_date": str(start + timedelta(days=2)),
                "budget": 60_000, "group_size": 1, "interests": ["history", "beaches"]}  # fmt: skip
        trip_id = http.post("/trips", json=body).json()["id"]
        began = time.perf_counter()
        assert http.post(f"/trips/{trip_id}/plan", json={"raw_input": "A relaxed long weekend"}).status_code == 202
        for _ in range(300):
            runs = http.get(f"/trips/{trip_id}/runs").json()
            if any(run["agent_name"] == "orchestrator" for run in runs):
                break
            time.sleep(1)
        print(f"A plan for Goa, {start}, three days — {time.perf_counter() - began:.1f} s")
        saved = http.get(f"/trips/{trip_id}/itinerary")
        if not checks.expect(saved.status_code == 200, "a plan was made", f"HTTP {saved.status_code}"):
            return
        plan = saved.json()["structured_data"]

    stops, noted, far, bad = 0, 0, 0, []
    for day in plan["days"]:
        print(f"     Day {day['day']} ({day['date']}), sleeping at {(day.get('hotel') or {}).get('name') or '—'}")
        timed = []
        for slot in SLOTS:
            stop = day.get(slot)
            if not stop:
                continue
            stops += 1
            note = stop.get("travel")
            way = f"  ← {travel_words(note)}" if note else ""
            print(
                f"       {slot:9} {stop.get('start_time') or '—':>5}–{stop.get('end_time') or '—':<5}  {stop['activity']}{way}"
            )
            if stop.get("start_time"):
                timed.append(stop)
            elif not stop.get("no_time_left"):
                bad.append(f"day {day['day']} {slot}: no times")
            noted += bool(note)
        for before, after in zip(timed, timed[1:]):
            if minutes(after["start_time"]) < minutes(before["end_time"]):
                bad.append(f"day {day['day']}: {after['activity']} starts before {before['activity']} ends")
        if timed and not (
            schedule.DAY_START <= minutes(timed[0]["start_time"]) and minutes(timed[-1]["end_time"]) <= schedule.DAY_END
        ):
            bad.append(f"day {day['day']}: outside 08:00–22:00")
    # what is far, as the schedule measures it: from where the traveller was
    for stop, here, _name, to in schedule._legs(plan):
        if schedule.straight_km(here, to) > schedule.FAR_KM:
            far += 1
            if not stop.get("travel"):
                bad.append(f"{stop['activity']}: more than 10 km away, and no travel note")
    checks.expect(
        stops > 0 and not bad,
        "every stop has a start and an end; no day before 08:00 or after 22:00; none overlap",
        "; ".join(bad),
    )
    checks.expect(
        noted == far, "every stop more than 10 km away has a travel note — and no other", f"{noted} notes, {far} far"
    )
    sources = {
        stop["travel"]["source"]
        for day in plan["days"]
        for slot in SLOTS
        if (stop := day.get(slot)) and stop.get("travel")
    }
    print(f"     the ways: {', '.join(sorted(sources)) or 'none far'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="A plan's clock, and the travel-time tool.")
    parser.add_argument("--route", action="store_true", help="Fort Aguada → Baga Beach on Google, twice")
    parser.add_argument("--prodlike", action="store_true", help="a plan on the production-like stack")
    parser.add_argument("--api", default="", help="a plan on this backend")
    args = parser.parse_args()
    checks = Checks()
    if args.route:
        the_route(checks)
    if args.prodlike or args.api:
        a_plan(checks, "http://localhost:8200" if args.prodlike else args.api.rstrip("/"))
    if not (args.route or args.prodlike or args.api):
        parser.error("say --route, --prodlike or --api")
    print(f"\n{checks.passed} passed, {checks.failed} failed")
    return 0 if not checks.failed else 1


if __name__ == "__main__":
    sys.exit(main())
