# Phase 15 — RefinementClassifier prompt

> Version: v1  
> Model: `gemini-1.5-flash`, `temperature=0`  
> Referenced by: `src/ai/agents/refinement_classifier.py`

---

## Purpose

This prompt classifies a user's follow-up message about an existing trip plan into one of **five action types** that determine which sub-agents the orchestrator will re-run.

The goal is to be as targeted as possible: re-run only the sub-agent(s) whose output the user wants changed, carry everything else forward unchanged.

---

## Classification Types

| Type | Re-runs | Carries forward | Example triggers |
|---|---|---|---|
| `full_replan` | All three agents | Nothing | "I'd rather go to Mumbai", "start over", date change |
| `targeted_flights` | FlightAgent only | Hotels + activities | "Make it cheaper", "find a direct flight", "non-stop only" |
| `targeted_hotel` | HotelAgent only | Flights + activities | "Closer to the beach", "switch to a 5-star", "something with a pool" |
| `targeted_activities` | ActivitiesAgent only | Flights + hotels | "Swap food for shopping", "add more history", "less adventure" |
| `add_day` | All three agents | Nothing (date change) | "Add a day", "make it 8 days", "one more night" |

---

## Hard Rules (always apply regardless of phrasing)

1. **"Make it cheaper"** → `targeted_flights`  
   Flights are the single largest cost lever. Hotel reductions require the user to mention hotels explicitly.

2. **Any duration extension** → `add_day`  
   Date changes invalidate both flights (different route pricing) and hotels (different availability). Always re-run all three.

3. **Any destination change** → `full_replan`  
   A new destination invalidates every previous search result. There is no partial carry-forward when the destination changes.

---

## Hard Cases & Rationale

| Message | Classification | Why |
|---|---|---|
| "Make it cheaper" | `targeted_flights` | Cheapest fix for budget is a different flight, not a hotel change |
| "Add a day" | `add_day` | Date extension → new flight prices + new hotel availability |
| "I'd rather go to Manali" | `full_replan` | Destination change invalidates everything |
| "Find a non-stop flight" | `targeted_flights` | Only the flight routing needs to change |
| "Something with a pool" | `targeted_hotel` | Pool is a hotel attribute; flights/activities unaffected |
| "Less adventure, more food" | `targeted_activities` | Only interest profile changes |
| "Let's do it in February instead" | `full_replan` | Date change → fresh search needed for all three agents |

---

## Failure Behaviour

If the LLM returns invalid JSON or a type not in the five allowed values, the classifier falls back to `full_replan`. This is the safe default: it re-derives everything from scratch rather than silently using stale data from the wrong agent.
