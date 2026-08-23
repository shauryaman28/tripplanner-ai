# ItineraryBuilder System Prompt — v1

> Phase 12 — Baseline structured synthesis via Claude Haiku 4.5

## v1 (Baseline schema + data-scope rule)

- **Design:** Single Claude Haiku call receives trip meta + flights/hotels/
  attractions JSON and must return the exact day-by-day schema from the
  roadmap. Prompt states the data-scope rule ("only reference names given to
  you") and the budget rule ("total_cost must equal sum of day costs") up
  front, in the system prompt, not buried in the user message.
- **What it does well:** Correctly reproduces the JSON shape on well-formed
  input; correctly uses the "Explore the area" fallback when attractions is
  empty for a day (tested with an empty attractions list).
- **Where it fails:** On 2 of 5 initial test runs, the model invented a
  plausible-sounding but non-existent hotel name when `hotels` had zero
  results (rather than emitting `hotel: null`). The Python-side data-scope
  validator (`_validate_data_scope`) catches this every time regardless of
  prompt quality — that is intentional defense-in-depth, not a
  workaround for the prompt.
- **Next version (v2):** Add an explicit "if a list is empty, do not invent
  an entry — set the field to null" rule, and add worked examples for the
  empty-data case.
