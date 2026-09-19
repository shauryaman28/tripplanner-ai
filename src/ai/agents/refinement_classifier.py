"""Phase 15 — RefinementClassifier: classifies refinement messages into action types.

This module is the core of the multi-turn refinement loop. On turn 2+,
the user sends a message like "change hotels to something closer to the
beach" rather than planning from scratch. The classifier decides which
sub-agent(s) need to re-run and which results can be carried forward.

Classification types
────────────────────
  full_replan         All three agents re-run; previous state is discarded.
                      Triggers on: destination change, date change, "start over",
                      "I'd rather go to Mumbai".

  targeted_flights    Only FlightAgent re-runs; hotel + activities carried forward.
                      Triggers on: "make it cheaper", "find a direct flight",
                      "I need an earlier departure", "non-stop only".

  targeted_hotel      Only HotelAgent re-runs; flights + activities carried forward.
                      Triggers on: "closer to the beach", "change hotel", "more
                      luxurious", "switch to a 5-star hotel".

  targeted_activities Only ActivitiesAgent re-runs; flights + hotels carried forward.
                      Triggers on: "swap food activities for shopping", "add more
                      history", "less adventure, more relaxation".

  add_day             All three agents re-run with an extended date range.
                      Triggers on: "add a day", "make it 8 days", "one more night".

Hard cases (documented in prompts/refinement_classifier_v1.md and tested):
  "Make it cheaper"      → targeted_flights (flights are the largest cost lever)
  "Add a day"            → add_day (date extension requires fresh hotel + flight search)
  "I'd rather go to X"  → full_replan (destination change invalidates all results)
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── Output model ──────────────────────────────────────────────────────────

RefinementType = Literal[
    "full_replan",
    "targeted_flights",
    "targeted_hotel",
    "targeted_activities",
    "add_day",
]


class RefinementClassification(BaseModel):
    refinement_type: RefinementType
    reason: str  # one-line explanation, useful for logging and tests


# ── Prompt ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a travel planning assistant classifying a user's follow-up \
message about an existing trip itinerary.

Classify the message into exactly one of these types and return ONLY a JSON object:

{{
  "refinement_type": "<one of the five types below>",
  "reason": "<one-line explanation>"
}}

Classification types:
  full_replan         — The user wants to change the destination, travel dates, or \
start completely over. Previous results are invalid. Examples: "I'd rather go to \
Mumbai", "let's do it in January instead", "start over".

  targeted_flights    — The user wants different flights only; hotels and activities \
can stay. Examples: "make it cheaper", "find a direct flight", "earlier departure", \
"non-stop only", "I prefer IndiGo".

  targeted_hotel      — The user wants a different hotel only; flights and activities \
can stay. Examples: "closer to the beach", "switch to a nicer hotel", "something with \
a pool", "change to a 5-star", "beachfront property".

  targeted_activities — The user wants different activities only; flights and hotels \
can stay. Examples: "swap food tours for shopping", "add more history", "less \
adventure", "I prefer cultural experiences".

  add_day             — The user wants to extend the trip by one or more days. All \
agents must re-run with the updated date range. Examples: "add a day", "make it 8 \
days", "one more night", "extend by two days".

Hard rules (always apply, regardless of phrasing):
  1. "Make it cheaper" → targeted_flights (flights are the biggest cost lever).
  2. "Add a day" or any duration extension → add_day (date changes invalidate flights \
and hotels).
  3. Any destination change → full_replan (invalidates everything).

Return ONLY the JSON object. No explanation, no markdown fences.

Conversation history so far:
{history}

New user message: {message}"""


# ── Classifier function ───────────────────────────────────────────────────


async def classify_refinement(
    message: str,
    conversation_history: list[dict],
) -> RefinementClassification:
    """Classify a refinement message using Gemini Flash at temperature=0.

    Returns a RefinementClassification. On any LLM or parse failure, falls
    back to "full_replan" — the safest default (re-derives everything from
    scratch rather than silently using stale data).
    """
    history_text = "\n".join(
        f"[Turn {e.get('turn', 1)}] {e['role'].upper()}: {e['content']}"
        for e in conversation_history[-10:]  # last 10 messages for context
    ) or "(none)"

    prompt = _SYSTEM_PROMPT.format(history=history_text, message=message)

    try:
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)
        response = await llm.ainvoke(prompt)
        text = response.content.strip()

        if text.startswith("```"):
            text = text.split("```")[1].removeprefix("json").strip()

        parsed = json.loads(text)
        return RefinementClassification(**parsed)

    except Exception as exc:
        logger.warning(
            "RefinementClassifier failed (%s) — defaulting to full_replan", exc
        )
        return RefinementClassification(
            refinement_type="full_replan",
            reason=f"Classification failed ({exc}); safe fallback.",
        )
