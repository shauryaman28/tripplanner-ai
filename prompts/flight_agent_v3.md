# FlightAgent System Prompt — v3

> Phase 7B — Multi-Turn Clarification & Retry

## What changed from v2

v2 extracted fields from a single user message. v3 supports multi-turn
clarification: when the router returns `"clarify"`, the user's answer is
stored in Redis and fed back through intent parsing. The key fix is in the
field-filling logic.

## The critical fix

v2 field check: `if value is not None and field not in state`

This breaks on retry. After the first `/plan` call returns a clarification,
the saved state looks like `{"date": None, "budget": None, ...}`. Both keys
**exist** — they're just `None`. On `/clarify`, intent parsing extracts
`date` from the user's answer but then checks `"date" not in state` — that's
`False` (key exists), so it **never writes** the extracted value. The router
sees `date: None` and clarifies again **forever**.

v3 field check: `if value is not None and not state.get(field)`

This treats `None` values as "fill this in", which is the correct semantic.

## Prompt text

The `_INTENT_PROMPT` is unchanged from v2. The improvement is structural —
how state flows through the graph on retry — not in the prompt wording.

## What it does well

1. **Multi-turn works** — user answers "December 15" → date is correctly
   filled on the second run without losing previously extracted fields.
2. **Partial state preserved** — `destination: "GOI"` survives across the
   clarify round-trip via Redis state storage.
3. **Conversation history** — `conversation_history: list[dict]` added to
   `TripState` for future multi-turn context injection into prompts.

## Where it fails

1. **No history injection** — `conversation_history` is stored but not yet
   injected into the intent-parsing prompt. The LLM sees only the current
   `raw_input`, not the full conversation. This is acceptable for Phase 7B
   (the router + field-filling logic handles correctness) but limits the
   LLM's ability to resolve pronouns ("there" → previously mentioned city).
2. **Single-field clarification** — `clarify_node` asks about only the
   **first** missing field. If 2 fields are missing, the user goes through
   2 rounds. A smarter prompt could ask about multiple fields at once.

## API contract (Phase 7B)

- `POST /trips/{id}/plan` with `raw_input` → may return `{"status": "clarification_needed", "question": "..."}`
- `POST /trips/{id}/clarify` with `{"answer": "..."}` → re-runs the graph with merged state
- State and conversation history persisted in Redis with 24h TTL

## Next version

Phase 15 adds `RefinementClassifierNode` — conversation history will be
injected into the prompt for multi-turn refinement ("change hotels to
something closer to the beach").
