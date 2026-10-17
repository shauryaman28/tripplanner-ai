"""Ask a running backend for trips in Hindi, Tamil and Bengali — and one in English — and say what came back (Phase 36).

    python scripts/language_check.py --prodlike            # the production-like stack (http://localhost:8200)
    python scripts/language_check.py --api http://localhost:8000
    python scripts/language_check.py --prodlike --all      # …and Tamil, Bengali, a language chosen, a change typed in Hindi

    A  the roadmap's request, "गोवा में 5 दिन की यात्रा, बजट ₹50,000", the destination typed "गोवा"
       → planned in Hindi: every stop described in Hindi and named in Devanagari; every name the plan goes by
         English; the tips in Hindi; the trip still called "गोवा"
    E  an English request, nothing chosen → no `language` or `localise` step, no new key in the plan
  --all adds:
    B, C  a request in Tamil, one in Bengali → planned in each
    D     Hindi chosen in the preferences, the request in English → planned in Hindi, nothing translated
    F     A changed by a request typed in Hindi → still Hindi, every stop it kept with the words it had

These are real plans: on real keys each asks the models (a translation and a localisation more than an English
plan) — Gemini's free tier allows 20 requests a day for each model, and Groq's 8,000 tokens a minute; --all asks
for six plans, and on the free tier some of their calls are turned away and asked again. It makes two accounts.
Exit code 0 when every check held.
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "backend"))

from src.ai import language  # noqa: E402

SLOTS = ("morning", "afternoon", "evening")
START = date.today() + timedelta(days=40)


class Checks:
    def __init__(self) -> None:
        self.passed = self.failed = 0

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        self.passed, self.failed = self.passed + bool(ok), self.failed + (not ok)
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  — {detail}" if detail else ""), flush=True)
        return bool(ok)


def sign_up(http: httpx.Client) -> None:
    creds = {"email": f"language-check-{secrets.token_hex(4)}@example.com", "password": secrets.token_urlsafe(14)}
    assert http.post("/auth/register", json=creds).status_code == 201, "could not make an account"
    login = http.post("/auth/login", data={"username": creds["email"], "password": creds["password"]})
    http.headers["Authorization"] = f"Bearer {login.json()['access_token']}"


def finished(http: httpx.Client, trip_id: str, runs: int) -> list[dict]:
    for _ in range(300):
        rows = http.get(f"/trips/{trip_id}/runs").json()
        if sum(row["agent_name"] == "orchestrator" for row in rows) >= runs:
            return rows
        time.sleep(1)
    raise TimeoutError(f"trip {trip_id}: no run finished in five minutes")


def planned(http: httpx.Client, destination: str, raw_input: str, days: int = 3) -> tuple[str, list[dict], float]:
    body = {"destination": destination, "start_date": str(START), "end_date": str(START + timedelta(days=days - 1)),
            "budget": 50_000, "group_size": 1}  # fmt: skip
    trip_id = http.post("/trips", json=body).json()["id"]
    began = time.perf_counter()
    assert http.post(f"/trips/{trip_id}/plan", json={"raw_input": raw_input}).status_code == 202
    return trip_id, finished(http, trip_id, 1), time.perf_counter() - began


def stops_of(plan: dict) -> list[dict]:
    return [day[slot] for day in plan.get("days", []) for slot in SLOTS if day.get(slot)]


def in_language(
    checks: Checks, http: httpx.Client, trip_id: str, rows: list[dict], code: str, typed: str | None
) -> dict:
    plan = http.get(f"/trips/{trip_id}/itinerary").json()["structured_data"]
    stops = stops_of(plan)
    places = [stop for stop in stops if stop["activity"] != "Explore the area"]
    name = language.SUPPORTED[code].name
    checks.expect(plan.get("language") == code, f"the plan says it is in {name}", f"language: {plan.get('language')}")
    described = sum(language.has_script(stop.get("description") or "", code) for stop in stops)
    checks.expect(described == len(stops) > 0, f"every stop is described in {name}", f"{described} of {len(stops)}")
    named = sum(language.has_script(stop.get("local_name") or "", code) for stop in places)
    checks.expect(named == len(places), f"every place has its name in {name}'s script", f"{named} of {len(places)}")
    english = all(not any(language.has_script(stop["activity"], c) for c in language.SUPPORTED) for stop in stops)
    checks.expect(english, "every name the plan goes by is still English")
    tips = (plan.get("local_intelligence") or {}).get("local_transport")
    if tips:
        checks.expect(language.has_script(tips, code), f"the local tips are in {name}", tips[:70])
    localised = next((row for row in rows if row["agent_name"] == "localise"), None)
    if localised:
        kept = localised["output"].get("kept_in_english")
        checks.expect(not kept, "nothing the localise step was given stayed English", f"kept_in_english: {kept}")
    if typed:
        now = http.get(f"/trips/{trip_id}").json()["destination"]
        checks.expect(now == typed, "the trip keeps the destination as it was typed", now)
    if places:
        print(
            f"     e.g. {places[0]['activity']} → {places[0].get('local_name')}: {(places[0].get('description') or '')[:80]}"
        )
    return plan


def read_as(checks: Checks, rows: list[dict], code: str, decided_by: str) -> None:
    row = next((row for row in rows if row["agent_name"] == "language"), None)
    if not checks.expect(
        row is not None, "the request's language was read", "no `language` row" if row is None else ""
    ):
        return
    out = row["output"]
    checks.expect(
        (out.get("language"), out.get("decided_by")) == (code, decided_by),
        f"planned in {code}, decided by the {decided_by}",
        f"{out.get('language')}, by the {out.get('decided_by')}; translated: {out.get('translated')}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Trips in Hindi, Tamil and Bengali, as a traveller asks for them.")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--prodlike", action="store_true", help="the production-like stack")
    parser.add_argument("--all", action="store_true", help="Tamil, Bengali, a chosen language and a change too")
    args = parser.parse_args()
    api = "http://localhost:8200" if args.prodlike else args.api.rstrip("/")
    checks = Checks()
    print(api)
    with httpx.Client(base_url=api, timeout=60) as http:
        sign_up(http)
        print("\nA. The roadmap's request, in Hindi")
        hindi, rows, took = planned(http, "गोवा", "गोवा में 5 दिन की यात्रा, बजट ₹50,000", days=5)
        print(f"     planned in {took:.1f} s")
        read_as(checks, rows, "hi", "request")
        in_language(checks, http, hindi, rows, "hi", typed="गोवा")

        if args.all:
            for code, destination, request in (
                ("ta", "மதுரை", "சென்னையிலிருந்து மதுரைக்கு மூன்று நாள் சுற்றுலா, கோயில்கள் பார்க்க வேண்டும்"),
                ("bn", "কলকাতা", "কলকাতায় তিন দিন, পুরনো বাড়ি, জাদুঘর আর মিষ্টির দোকান দেখতে চাই"),
            ):
                print(f"\n{'B' if code == 'ta' else 'C'}. A request in {language.SUPPORTED[code].name}")
                trip_id, rows, took = planned(http, destination, request)
                print(f"     planned in {took:.1f} s")
                read_as(checks, rows, code, "request")
                in_language(checks, http, trip_id, rows, code, typed=destination)

            print("\nF. The Hindi plan, changed by a request typed in Hindi")
            before = language.local_text_of(http.get(f"/trips/{hindi}/itinerary").json()["structured_data"], "hi")
            assert http.post(f"/trips/{hindi}/refine", json={"message": "होटल समुद्र तट के पास चाहिए"}).status_code == 200
            rows = [row for row in finished(http, hindi, 2) if row["turn"] == 2]
            read_as(checks, rows, "hi", "request")
            plan = in_language(checks, http, hindi, rows, "hi", typed="गोवा")
            after = language.local_text_of(plan, "hi")
            kept = [stop for stop in after if stop in before and after[stop] == before[stop]]
            checks.expect(len(kept) == len([stop for stop in after if stop in before]), "every stop it kept kept its words",
                          f"{len(kept)} of {len([stop for stop in after if stop in before])}")  # fmt: skip

    with httpx.Client(base_url=api, timeout=60) as http:
        sign_up(http)
        if args.all:
            print("\nD. Hindi chosen, the request in English")
            http.put("/users/preferences", json={"preferred_language": "hi"})
            trip_id, rows, took = planned(http, "Jaipur", "Three relaxed days in Jaipur, forts and food")
            print(f"     planned in {took:.1f} s")
            read_as(checks, rows, "hi", "preference")
            in_language(checks, http, trip_id, rows, "hi", typed=None)
            http.put("/users/preferences", json={})

        print("\nE. A request in English, nothing chosen")
        trip_id, rows, took = planned(http, "Udaipur", "Three relaxed days in Udaipur, lakes and palaces")
        print(f"     planned in {took:.1f} s")
        steps = {row["agent_name"] for row in rows}
        checks.expect(
            not steps & {"language", "localise"},
            "no step of this phase ran",
            ", ".join(sorted(steps & {"language", "localise"})),
        )
        plan = http.get(f"/trips/{trip_id}/itinerary").json()["structured_data"]
        new = {key for stop in stops_of(plan) for key in stop} & {"description", "local_name"}
        checks.expect("language" not in plan and not new, "the plan has no key it did not have before", ", ".join(new))

    print(f"\n{checks.passed} passed, {checks.failed} failed")
    return 0 if not checks.failed else 1


if __name__ == "__main__":
    sys.exit(main())
