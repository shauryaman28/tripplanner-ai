# OrchestratorAgent System Prompt — v1

> Phase 9 — Baseline intent extraction

## v1 (Phase 9 — Initial structured extraction)

- **Design:** Gemini Flash extracts all seven trip fields from free-form text in one call.
- **What it does well:** Handles basic queries ("Plan a 7-day trip to Goa in December for 2 people, budget ₹50,000") correctly.
- **Where it fails:**
  1. "Around ₹50k" → sometimes extracts `50` instead of `50000` (missing unit inference).
  2. "Next month" without a year → picks current year even when that's in the past.
  3. "Family holiday" → group_size left null (doesn't infer from "family").
  4. Mixed language queries ("Goa trip के लिए ₹50,000") → interests field sometimes null.

## Prompt text (v1)

See `_INTENT_PROMPT` in `src/ai/orchestrator/orchestrator.py`.

## Ask vs. assume policy

- **destination absent:** set null, do NOT guess. Downstream: sub-agents will fail with UNKNOWN_DESTINATION; route surfaces this via SSE.
- **dates ambiguous:** set start_date to first day of mentioned month, end_date null. ItineraryBuilder will use trip.end_date from DB as fallback.
- **budget unspecified:** null. estimate_budget will be called with conservative defaults.
- **group_size unspecified:** null (caller defaults to 1).

## Versioning convention

Each version documents:
1. What the prompt says / what changed
2. Test cases run (5 minimum)
3. Specific failure modes fixed
4. What's still broken
