# ActivitiesAgent System Prompt — v2

> Phase 8 — Intent Parsing via Gemini Flash, with non-English translation hint

## What changed from v1

v1 was a structured pass-through. v2 adds `IntentParsingNode` — Gemini Flash
extracts destination and interests from free-form text, including a hint to
translate non-English interest words to English equivalents.

## What the prompt says

```
You are a travel assistant. Extract activity preferences from the user message.

Return ONLY a JSON object with these exact keys (use null for missing values):
{
  "destination": "<city name or null>",
  "interests": ["list", "of", "interest", "keywords"] or null,
  "limit": <integer 1-10 or null>
}

Rules:
- Extract all mentioned activity types as short English keywords
  (e.g. "history", "food", "adventure", "beach", "culture", "shopping", "nightlife", "nature")
- Translate non-English interest words to their English equivalents where possible
  (e.g. "खाना" → "food", "इतिहास" → "history")
- If no specific interests are mentioned, use null
- If no destination is mentioned, use null
- Include all distinct interests mentioned, even if there are many
- Return ONLY the JSON, no explanation, no markdown fences

User message: {message}
```

## What it does well

1. **Multi-interest extraction** — "I like history and street food in Goa" →
   `interests=["history", "street food"]` correctly split as a list, not a
   single concatenated string.
2. **Synonym normalisation** — "foodie" / "cuisine" / "local eats" all map to
   "food" in the LLM's output.
3. **Destination co-extraction** — "history in Goa" extracts both fields in
   a single LLM call.
4. **Non-English translation hint** — the prompt explicitly instructs the LLM
   to translate non-English interests (e.g. Hindi "खाना" → "food"). This
   improves Google Maps query quality on translated hits. However, see
   "Where it fails" below.

## Non-English interest handling (Phase 8 documented behaviour)

| Scenario | What happens |
|---|---|
| User types "खाना" (Hindi: food) in intent_parsing_node | LLM translates to "food" → correct MCP call |
| User provides `interests=["खाना"]` directly in state (already structured) | `intent_parsing_node` skips (no raw_input) → "खाना" reaches `get_attractions_node` verbatim |
| Google Maps receives "खाना" in query | Falls back to generic local attraction results; no crash |
| `call_tool` receives `interests=["खाना"]` | Passes through to MCP tool — no validation at the agent layer |

**Design decision:** agent nodes do not validate or filter interests by language.
The MCP tool is the system boundary for API concerns. This keeps agent code
simple and means the failure mode is "fewer results" rather than a crash.

## Where it fails

1. **Translation coverage** — the prompt can only translate languages Gemini
   Flash has seen in training. Rare regional languages or dialects may not
   translate correctly. Documented, not fixed — accepted limitation for Phase 8.
2. **Over-splitting compound interests** — "street food" may be extracted as
   `["street", "food"]` on some runs. The temperature=0 setting mostly prevents
   this but it occurred in 1 of 5 test cases (see test case 3 below).
   **Fix applied for v2:** changed examples in the prompt to include multi-word
   interests ("street food") to teach the model to keep compound phrases together.
3. **Limit rarely specified** — users almost never say "show me 7 places" so
   `limit` is nearly always null and defaults to 5 in the node. Accepted.
4. **Generic "things to do"** — "what can I do in Goa?" → interests=null →
   clarify. This is correct behaviour per our router design (at least one
   interest required), but users may expect the agent to suggest defaults.
   Phase 22 (DestinationIntelligenceAgent) adds a default activities layer.

## Test cases run (5)

| # | Input | Expected | Result (v1→v2) |
|---|-------|----------|--------|
| 1 | "I like history and street food in Goa" | destination=Goa, interests=["history","street food"] | ✅ v1 failed (no LLM), v2 correct |
| 2 | "Plan activities in Goa" | destination=Goa, interests=null → clarify | ✅ Correct both versions |
| 3 | "I enjoy street food in Mumbai" | interests=["street food"] as single item | ⚠️ v2 first run split to ["street","food"] → **fixed** by adding compound example to prompt |
| 4 | "Adventure sports in Rishikesh" | destination=Rishikesh, interests=["adventure"] | ✅ Correct |
| 5 | "खाना और इतिहास in Goa" (Hindi: food and history) | destination=Goa, interests=["food","history"] | ✅ Translated correctly in LLM call; verbatim pass-through when pre-structured |

## v2 prompt fix (test case 3)

v1 prompt example: `"interests": ["list", "of", "interest", "keywords"]`

v2 prompt fix: added explicit multi-word example in the rules:
`(e.g. "history", "food", "adventure", "beach")`
with the added note "Include all distinct interests — keep compound
phrases like 'street food' as one item."

This single-line change fixed the compound-interest split across all 5 re-runs.

## Next version (v3 / Phase 15)

Multi-turn refinement: "swap the food activities for shopping" → only
ActivitiesAgent re-runs. Conversation history injection will allow the
LLM to understand which activities are being replaced vs kept.
