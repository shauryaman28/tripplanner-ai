# FlightAgent System Prompt — v1

> Placeholder created in Phase 2. Content written in Phase 6 when FlightAgent is built.
> This file establishes the versioning habit: every LLM prompt gets a versioned log.

## v1 (Phase 6 — Structured Input Pass-through)

- **Design:** In Phase 6, `FlightAgent` accepts structured inputs directly (`destination`, `origin`, `date`, `budget`, `passengers`) and passes parameters directly to `search_flights` tool via MCP client without an active LLM call.
- **Rationale:** Free-form text intent parsing ("somewhere warm in December") is handled in Phase 7 by `IntentParsingNode`. Adding an LLM prompt here when inputs are already structured would be unnecessary overhead.
- **What it does well:** 100% deterministic parameter forwarding with zero LLM latency/hallucination risk.
- **Where it fails:** Cannot parse raw unstructured natural language queries without pre-extracted parameters.
- **Next Version (v2 / Phase 7):** Introduce `IntentParsingNode` prompt to convert unstructured user text into structured `TripState`.

## Versioning convention
Each version documents:
1. What the prompt says
2. What it does well
3. Where it fails
4. What changed going into the next version
