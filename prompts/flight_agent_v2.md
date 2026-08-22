# FlightAgent System Prompt — v2

> Phase 7 — Intent Parsing via Gemini Flash

## What changed from v1

v1 was a structured pass-through: caller provided all fields, no LLM involved.
v2 adds `IntentParsingNode` — a Gemini Flash call that extracts structured fields
from free-form user text before the search/clarify routing decision.

## What the prompt says

```
You are a travel assistant. Extract flight search fields from the user message.

Return ONLY a JSON object with these exact keys (use null for missing values):
{
  "origin": "<IATA code or null>",
  "destination": "<IATA code or null>",
  "date": "<YYYY-MM-DD or null>",
  "budget": <number in INR or null>,
  "passengers": <integer or null>
}

Rules:
- Convert city names to IATA codes (Delhi=DEL, Mumbai=BOM, Goa=GOI, Bangalore=BLR)
- Convert relative dates using today as reference: {today}
- If a field is not mentioned at all, use null
- Return ONLY the JSON, no explanation
```

## What it does well

1. **IATA conversion** — correctly maps common Indian cities to airport codes.
2. **Relative date handling** — "next Friday" / "in December" resolved using `{today}` injection.
3. **Strict JSON output** — `temperature=0` + explicit schema keeps output parseable.
4. **Graceful degradation** — if JSON parsing fails, state passes through unchanged and the router sends to `clarify` (missing fields → clarification question).

## Where it fails

1. **Limited IATA mapping** — only 4 cities hardcoded in prompt. "Jaipur" or "Kolkata" may not get correct codes. Fix: expand the mapping or use a lookup table.
2. **Ambiguous dates** — "December" without a year → model may pick current year even if it's already past. Partially mitigated by `{today}` injection.
3. **Currency ambiguity** — "50k budget" is assumed INR but "500 dollars" might confuse it. No explicit currency normalisation in the prompt.
4. **Multi-leg trips** — "Delhi to Goa then Goa to Mumbai" is unsupported; only one origin/destination pair extracted.

## Test cases run (5)

| # | Input | Expected | Result |
|---|-------|----------|--------|
| 1 | "Fly from Delhi to Goa on Dec 10, budget 20000, 2 people" | All fields extracted | ✅ Correct |
| 2 | "I want to go to Goa" | destination=GOI, rest null → clarify | ✅ Correct |
| 3 | "Somewhere warm in December" | All null → clarify | ✅ Correct (destination null) |
| 4 | "Mumbai to Bangalore next Friday, 15k" | origin=BOM, dest=BLR, date resolved, budget=15000 | ✅ Correct |
| 5 | "Plan a trip for 3 people" | passengers=3, rest null → clarify | ✅ Correct |

## Next version (v3 / Phase 7B)

Multi-turn clarification: when the router returns "clarify", the user's answer
is fed back through intent parsing on retry. The `field not in state` check
must become `not state.get(field)` to handle None-valued keys on retry.
