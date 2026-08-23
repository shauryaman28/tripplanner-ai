# HotelAgent System Prompt — v1

> Placeholder created at Phase 8 start. Content written when HotelAgent is built.
> Follows the same versioning discipline as FlightAgent prompts.

## v1 (Phase 8 — Structured Input Pass-through)

- **Design:** In Phase 8 v1, `HotelAgent` accepts structured inputs directly
  (`destination`, `check_in`, `check_out`, `budget_per_night`, `guests`) and
  passes parameters directly to `search_hotels` via MCP client without an
  active LLM call.
- **Rationale:** Same reasoning as `flight_agent_v1.md` — structured inputs
  don't need interpretation. Adding an LLM here when inputs are already
  structured would be unnecessary overhead with zero benefit.
- **What it does well:** 100% deterministic parameter forwarding, no latency,
  no hallucination risk.
- **Where it fails:** Cannot parse raw natural language queries like
  "I need a hotel in Goa from 10th to 17th December, budget ₹5,000 per night
  for 2 people" without pre-extracted parameters.
- **Next version (v2):** Introduce `IntentParsingNode` to convert unstructured
  hotel search requests into structured `HotelState`.

## Versioning convention

Each version documents:
1. What the prompt says (or what the node does, for pass-through versions)
2. What it does well
3. Where it fails
4. What changed going into the next version

**Critical: hotel-specific reasoning is kept strictly separate from
FlightAgent prompts.** Hotels require different field semantics:
- `check_in` / `check_out` vs. `date` / `return_date`
- `budget_per_night` vs. total `budget`
- `guests` vs. `passengers`

Copying FlightAgent's prompt and renaming fields is a source of subtle bugs
that are hard to catch in tests but visible in production failures.
