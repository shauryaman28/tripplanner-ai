"""What an English plan sent to every model, and what it made — captured before Phase 36 changed anything.

    python -m tests.fixtures.phase36.capture_english          # rewrites english_plan.json

Run on the commit before Phase 36 (55e1b2a): the file it wrote is the record that
tests/unit/test_phase36_language.py holds every later English plan to. Running it again
afterwards must change nothing in the file — that is the claim "English sees zero change".
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "src/backend")

HERE = Path(__file__).resolve().parent
OUT = HERE / "english_plan.json"

# Fixed dates (the stubbed flights are on 2030-01-10), so the record does not move with the calendar.
TRIPS = {
    "solo": {
        "destination": "Goa",
        "start_date": "2030-01-10",
        "end_date": "2030-01-12",
        "budget": 60_000.0,
        "group_size": 1,
        "interests": ["history", "beaches"],
        "raw_input": "A relaxed long weekend in Goa, forts and beaches, budget 60k",
    },
    "group": {
        "destination": "Goa",
        "start_date": "2030-01-10",
        "end_date": "2030-01-13",
        "budget": 120_000.0,
        "group_size": 3,
        "interests": [],
        "group_members": [
            {"name": "Asha", "interests": ["beach"]},
            {"name": "Ben", "interests": ["history"]},
            {"name": "Chitra", "interests": ["food"]},
        ],
        "raw_input": "Four days in Goa for three friends",
    },
}


def _normalised(text: str) -> str:
    return re.sub(r"Today is \d{4}-\d{2}-\d{2}", "Today is <today>", text)


async def capture() -> dict:
    from src.ai.orchestrator.orchestrator import OrchestratorAgent
    from tests import fakes
    from tests.fakes import flight, models_answer, network_stubs

    record: dict = {}
    for name, trip in TRIPS.items():
        asked: list[dict] = []

        async def large(system: str, user: str) -> str:
            asked.append({"model": "large", "system": _normalised(system), "user": _normalised(user)})
            return await fakes._the_stand_ins_answer(system, user)

        def short(system: str, user: str) -> str:
            asked.append({"model": "short", "system": _normalised(system), "user": _normalised(user)})
            return "{}"

        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer(large=large, short=short):
            result = await OrchestratorAgent().run(dict(trip), trip_id=uuid.uuid4())
        record[name] = {
            "asked": asked,
            "draft_itinerary": result["draft_itinerary"],
            "state_keys": sorted(result),
        }
    return record


def main() -> int:
    record = asyncio.run(capture())
    OUT.write_text(json.dumps(record, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    print(
        f"wrote {OUT.relative_to(Path.cwd())}: "
        + ", ".join(f"{k} {len(v['asked'])} prompts" for k, v in record.items())
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
