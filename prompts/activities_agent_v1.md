# ActivitiesAgent System Prompt — v1

> Placeholder created at Phase 8 start. Content written when ActivitiesAgent is built.
> Follows the same versioning discipline as FlightAgent and HotelAgent prompts.

## v1 (Phase 8 — Structured Input Pass-through)

- **Design:** In Phase 8 v1, `ActivitiesAgent` accepts structured inputs
  directly (`destination`, `interests: list[str]`, `limit`) and passes
  parameters directly to `get_attractions` via MCP client without an active
  LLM call.
- **Rationale:** Same as FlightAgent v1 / HotelAgent v1 — structured inputs
  don't need interpretation.
- **What it does well:** Deterministic parameter forwarding, zero latency.
- **Where it fails:** Cannot parse "I love history and street food in Goa"
  into `interests=["history", "street food"]` without pre-extraction.
- **Next version (v2):** Introduce `IntentParsingNode` to extract interests
  from free-form text, including translation hints for non-English inputs.

## Key difference from FlightAgent / HotelAgent

The `router` for ActivitiesAgent has a stricter dual requirement:
both **destination** AND **at least one interest** must be present.
An empty `interests` list is treated the same as `None` — it would
produce a near-useless MCP query ("top  attractions in <city>").

This is the agent most expected to require prompt iteration because:
1. Interests are subjective and expressed in many ways ("I like trying
   local food" vs "foodie" vs "cuisine" vs "खाना")
2. Users often omit interests entirely ("plan activities in Goa")
3. Multiple interests in one sentence need to be parsed as a list,
   not a single concatenated string

## Versioning convention

Each version documents:
1. What the prompt says (or what the node does, for pass-through versions)
2. What it does well
3. Where it fails
4. What changed going into the next version
