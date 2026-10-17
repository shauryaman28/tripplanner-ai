"""Does each key in .env still open its provider? One cheap question each (Phase 35).

    python scripts/provider_keys.py              # every key that is set
    python scripts/provider_keys.py --env-file .env.production

For a key rotation: put the new keys in .env, run this, then delete the old keys at the providers and run
it again — the answers are the same, and nothing in the repository knew the old ones (secret_audit.py).
Each question costs no quota — a list of models, one airline, the list of countries, one place's name,
the weather in one city, one LangSmith project — but one: Google's Routes API is asked one real journey
(Fort Aguada to Baga Beach), one element of the 10,000 a month its route matrix gives free (Phase 37). It
answers 400 to an invalid key as well as to a bad request, so nothing cheaper says a key works.

    ✓ GROQ_API_KEY          Groq            200 in 312 ms
    ✗ OPENWEATHER_API_KEY   OpenWeather     401 — the key was refused (a new one can take up to two hours to work)
    – ANTHROPIC_API_KEY     Anthropic       not set

No key is ever printed, and no address with a key in it: a provider's error is its status code. Exit code
0 when every key that is set was accepted, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Question:
    setting: str
    provider: str
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, str]
    refused_means: str = "the key was refused"
    body: dict | None = None


def questions(key: dict[str, str]) -> list[Question]:
    """What each provider is asked, with the key where the app sends it (src/ai/mcp_server/tools.py, src/ai/llm.py)."""
    return [
        Question("GROQ_API_KEY", "Groq", "GET", "https://api.groq.com/openai/v1/models",
                 {"Authorization": f"Bearer {key.get('GROQ_API_KEY', '')}"}, {}),
        Question("GOOGLE_API_KEY", "Gemini", "GET", "https://generativelanguage.googleapis.com/v1beta/models",
                 {"x-goog-api-key": key.get("GOOGLE_API_KEY", "")}, {"pageSize": "1"}),
        Question("DUFFEL_ACCESS_TOKEN", "Duffel", "GET", "https://api.duffel.com/air/airlines",
                 {"Authorization": f"Bearer {key.get('DUFFEL_ACCESS_TOKEN', '')}", "Duffel-Version": "v2"}, {"limit": "1"}),
        Question("LITEAPI_API_KEY", "LiteAPI", "GET", "https://api.liteapi.travel/v3.0/data/countries",
                 {"X-API-Key": key.get("LITEAPI_API_KEY", ""), "Accept": "application/json"}, {}),
        Question("OPENTRIPMAP_API_KEY", "OpenTripMap", "GET", "https://api.opentripmap.com/0.1/en/places/geoname",
                 {}, {"name": "Goa", "apikey": key.get("OPENTRIPMAP_API_KEY", "")}),
        Question("OPENWEATHER_API_KEY", "OpenWeather", "GET", "https://api.openweathermap.org/data/2.5/weather",
                 {}, {"q": "Goa,IN", "appid": key.get("OPENWEATHER_API_KEY", "")},
                 "the key was refused (a new one can take up to two hours to work)"),
        Question("LANGCHAIN_API_KEY", "LangSmith", "GET",
                 f"{(key.get('LANGCHAIN_ENDPOINT') or 'https://api.smith.langchain.com').rstrip('/')}/api/v1/sessions",
                 {"x-api-key": key.get("LANGCHAIN_API_KEY", "")}, {"limit": "1"}),
        Question("ANTHROPIC_API_KEY", "Anthropic", "GET", "https://api.anthropic.com/v1/models",
                 {"x-api-key": key.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"}, {"limit": "1"}),
        Question("GOOGLE_MAPS_API_KEY", "Google Maps", "POST",
                 "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix",
                 {"X-Goog-Api-Key": key.get("GOOGLE_MAPS_API_KEY", ""), "X-Goog-FieldMask": "duration,condition"}, {},
                 "the key was refused — is the Routes API enabled for its project, and billing on?",
                 {"origins": [{"waypoint": {"location": {"latLng": {"latitude": 15.4925, "longitude": 73.7735}}}}],
                  "destinations": [{"waypoint": {"location": {"latLng": {"latitude": 15.5553, "longitude": 73.7517}}}}],
                  "travelMode": "DRIVE"}),
    ]  # fmt: skip


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("'\"")
    return values


def ask(question: Question, client: httpx.Client) -> tuple[bool, str]:
    """(accepted, what to say). Never the key, never the address: a status code and a time."""
    started = time.perf_counter()
    try:
        answer = client.request(
            question.method, question.url, headers=question.headers, params=question.params, json=question.body
        )
    except httpx.HTTPError as failed:
        return False, f"could not be asked: {type(failed).__name__}"
    took = (time.perf_counter() - started) * 1000
    if answer.status_code == 200:
        return True, f"200 in {took:.0f} ms"
    if answer.status_code in (401, 403) or (answer.status_code == 400 and question.body is not None):
        return False, f"{answer.status_code} — {question.refused_means}"
    if answer.status_code == 429:
        return True, "429 — the key was accepted, and is over its quota for now"
    return False, f"{answer.status_code} — not what a working key gets"


def main() -> int:
    parser = argparse.ArgumentParser(description="Does each key in .env still open its provider?")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    key = read_env(args.env_file)
    failed = 0
    with httpx.Client(timeout=20) as client:
        for question in questions(key):
            if not key.get(question.setting) or key[question.setting].startswith("standin-"):
                print(f"  – {question.setting:<22} {question.provider:<12} not set")
                continue
            accepted, said = ask(question, client)
            failed += not accepted
            print(f"  {'✓' if accepted else '✗'} {question.setting:<22} {question.provider:<12} {said}", flush=True)
    print("every key that is set was accepted" if not failed else f"{failed} not accepted")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
