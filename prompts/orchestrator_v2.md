# OrchestratorAgent System Prompt — v2

> Phase 9 — Budget normalisation & date resolution fixes

## What changed from v1

Three targeted rule additions to the prompt:

1. **Budget normalisation rule:** added explicit examples — `"50k" → 50000`, `"2 lakhs" → 200000`. Tells the model to always output INR as a bare integer, never a string.
2. **Relative date anchor:** added `Today is {today}` injection and rule: "If only a month is given with no year, pick the next occurrence of that month from today."
3. **group_size inference:** added examples — `"solo" → 1`, `"couple" → 2`, `"family" → 4 (default if no number given)`.

## Test cases run (5)

| # | Input | v1 result | v2 result |
|---|-------|-----------|-----------|
| 1 | "Plan a 7-day trip to Goa in December for 2 people, budget ₹50,000" | ✅ All fields correct | ✅ Same |
| 2 | "Goa trip for a couple next month, around 50k" | ❌ budget=50, group_size=null | ✅ budget=50000, group_size=2 |
| 3 | "5 days in Jaipur starting Jan 10, family holiday" | ❌ group_size=null | ✅ group_size=4 |
| 4 | "Weekend getaway to Kerala, ₹15,000" | ❌ start_date=null (no explicit date) | ✅ start_date=next Saturday, end_date=next Sunday |
| 5 | "Mumbai to Goa flight and sightseeing, 3 nights" | ✅ destination=Goa, origin=Mumbai | ✅ Same |

## Still failing in v2

1. Mixed-language queries ("Goa trip के लिए ₹50,000 beach और food") — interests still sometimes null.
2. Multi-destination trips ("Goa then Jaipur") — only first destination extracted. Accepted limitation until Phase 25.

## What's fixed in v3
See `orchestrator_v3.md`.
