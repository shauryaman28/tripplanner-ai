"""The models' stand-ins: they answer as the real ones do, after as long as the real ones take.

The planner's own model-calling code is left in place — `ChatGroq(...)`, `ChatGoogleGenerativeAI(...)`,
`ainvoke`, `astream` — and what stands in is the class it instantiates. So a call still goes through
LangChain: its callbacks fire, its tokens are metered onto the agent's row, it is counted in
`llm_calls_total` and it is a span of the plan's trace. Nothing reaches a provider.

What each one says:

    the itinerary     a valid plan, made only from the flights, hotels and attractions in the prompt
                      — it must pass the builder's own checks and the evaluator's, as a real one must
    local tips        five kinds of tip about the destination in the prompt
    preferences       "mid-range", and nothing the traveller did not say
    a short prompt    intent parsing: nothing to add to a trip that names its fields;
                      the refinement classifier: a different hotel

What a stand-in leaves out is the work of talking to a provider: TLS, an HTTP client, a JSON
document per token. The itinerary is still handed over in pieces at the pace a model writes it,
because each piece is work for the app — it is batched, published to Redis and sent down every
open event stream.
"""

from __future__ import annotations

import asyncio
import json
import re
import zlib
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from loadtest.standins import faults, timing

EMBEDDING_DIMENSIONS = 1536
# An allowance for what a reasoning model spends before it answers, so that a build's row is of the
# size a real one is: 4,366 tokens for a reply of some 650 (docs/loadtest/real-plan-jaipur.json).
REASONING_TOKENS = 1500

NOTHING_TO_ADD = json.dumps(
    {
        "destination": None,
        "origin": None,
        "start_date": None,
        "end_date": None,
        "budget": None,
        "group_size": None,
        "interests": ["sightseeing"],
    }
)
A_DIFFERENT_HOTEL = json.dumps({"refinement_type": "targeted_hotel", "reason": "stand-in classifier"})
MID_RANGE = json.dumps(
    {"travel_style": "mid-range", "dietary_restrictions": [], "preferred_airlines": [], "home_city": None}
)


# ── What they say ──────────────────────────────────────────────────────────


def an_itinerary(user_prompt: str) -> str:
    """A plan for the trip in the builder's prompt, from the data in it and nothing else.

    Two attractions a day until they run out, then free days; one hotel, on every day but the
    last; the total added up as the builder's rules say — the cheapest flight, once, and a night's
    price per night. Written the way a model writes it — indented, each place's coordinates copied
    from the list as the prompt asks — which makes it about as long as a real one: some 2,400
    characters for four days, where a live one was 2,599.
    """
    start, end = (
        date.fromisoformat(found)
        for found in re.search(r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", user_prompt).groups()
    )
    last = (end - start).days
    flights, hotels, attractions = (
        json.loads(re.search(rf"Available {kind} \(JSON\): (\[.*?\])\n", user_prompt).group(1))
        for kind in ("flights", "hotels", "attractions")
    )
    hotel = {"name": hotels[0]["name"], "cost_per_night": hotels[0]["price_per_night_inr"]} if hotels else None

    def slot(index: int) -> dict | None:
        if index >= len(attractions):
            return None
        place = attractions[index]
        return {"activity": place["name"], "cost": 0, "lat": place.get("lat"), "lng": place.get("lng")}

    days = []
    for n in range(last + 1):
        morning, afternoon = slot(2 * n), slot(2 * n + 1)
        if morning is None:  # nothing left to do: a day gets one free-time slot
            morning = {"activity": "Explore the area", "cost": 0, "lat": None, "lng": None}
        days.append(
            {
                "day": n + 1,
                "date": str(start + timedelta(days=n)),
                "morning": morning,
                "afternoon": afternoon,
                "evening": None,
                "hotel": hotel if n < last else None,  # N days, N-1 nights
                "flight": None,
            }
        )
    total = min((flight["price_inr"] for flight in flights), default=0) + sum(
        day["hotel"]["cost_per_night"] for day in days if day["hotel"]
    )
    return json.dumps({"days": days, "total_cost": total, "currency": "INR"}, indent=4)


def local_tips(user_prompt: str) -> str:
    """What a local guide would say of the destination in the prompt — fenced, as models like to."""
    place = json.loads(user_prompt).get("destination") or "the destination"
    tips = {
        "local_transport": f"Autorickshaws and app cabs cover most of {place}; agree the fare before you get in.",
        "cultural_norms": [
            f"Shoes come off before entering a temple or a home in {place}.",
            "Ask before photographing people, and cover shoulders and knees at religious places.",
        ],
        "tourist_traps": [
            "Shops a driver insists on stopping at pay him a commission: say no, and go where you meant to.",
            "Fixed-price government emporiums are dearer than the market and cheaper than a tout.",
        ],
        "best_times": {
            f"{place} Fort (stand-in)": "Early morning, before the tour buses and the heat.",
            f"{place} Old Market (stand-in)": "After five, when the stalls are all open.",
            f"{place} Lakefront (stand-in)": "The hour before sunset.",
        },
        "safety_tips": [
            "Carry water and a hat: the middle of the day is hotter than a visitor expects.",
            "Use the prepaid taxi counter at the airport.",
        ],
    }
    return "```json\n" + json.dumps(tips, ensure_ascii=False) + "\n```"


def the_answer(system_prompt: str, user_prompt: str) -> tuple[str, timing.Wait]:
    """What a stand-in says to a prompt, and how long the real model takes to say it — by who is asking."""
    from src.ai.agents import destination_intelligence, preference_extractor
    from src.ai.builder import builder

    if system_prompt == builder._SYSTEM_PROMPT:
        return an_itinerary(user_prompt), timing.ITINERARY
    if system_prompt == destination_intelligence._SYSTEM_PROMPT:
        return local_tips(user_prompt), timing.LOCAL_TIPS
    if system_prompt == preference_extractor._SYSTEM_PROMPT:
        return MID_RANGE, timing.PREFERENCES
    if '"refinement_type"' in user_prompt:
        return A_DIFFERENT_HOTEL, timing.SHORT_PROMPT
    return NOTHING_TO_ADD, timing.SHORT_PROMPT


# ── The chat model ─────────────────────────────────────────────────────────


class StandInChatModel(BaseChatModel):
    """In place of ChatGroq and ChatGoogleGenerativeAI. `model` is the name it was asked for and answers by."""

    model: str

    @property
    def _llm_type(self) -> str:
        return "stand-in-chat-model"

    def _reply(self, messages: list[BaseMessage]) -> tuple[str, dict, timing.Wait]:
        system = next((str(message.content) for message in messages if message.type == "system"), "")
        user = next((str(message.content) for message in reversed(messages) if message.type == "human"), "")
        text, wait = the_answer(system, user)
        asked = sum(len(str(message.content)) for message in messages) // 4
        written = len(text) // 4 + (REASONING_TOKENS if wait is timing.ITINERARY else 0)
        usage = {"input_tokens": max(1, asked), "output_tokens": max(1, written)}
        return text, {**usage, "total_tokens": sum(usage.values())}, wait

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError("the planner only ever calls its models asynchronously")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        text, usage, wait = self._reply(messages)
        await asyncio.sleep(wait.seconds())
        message = AIMessage(content=text, usage_metadata=usage, response_metadata={"model_name": self.model})
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        """The reply in pieces: the model thinks, then writes — and the writing is what arrives over time."""
        text, usage, wait = self._reply(messages)
        writing = len(text) / timing.WRITING_CHARACTERS_A_SECOND * timing.scale
        await asyncio.sleep(max(0.0, wait.seconds() - writing))
        pieces = [text[at : at + timing.PIECE_CHARACTERS] for at in range(0, len(text), timing.PIECE_CHARACTERS)]
        between = writing / len(pieces) if pieces else 0.0
        for piece in pieces:
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if between:
                await asyncio.sleep(between)
        # the usage comes with the last piece, as Groq sends it
        last = AIMessageChunk(content="", usage_metadata=usage, response_metadata={"model_name": self.model})
        yield ChatGenerationChunk(message=last)


def _asking() -> str | None:
    """Whose work the model is being made for: the agent whose meter is open (src/ai/usage.py)."""
    from src.ai import usage

    return getattr(usage._meter.get(), "agent", None)


def groq(**options: Any) -> BaseChatModel:
    """What `ChatGroq(model=…, temperature=…, max_tokens=…, …)` makes while the stand-ins are in.

    Phase 31: with a fault on for it (faults.py), Groq's own client — given these very options —
    talking to a server that fails.
    """
    from src.ai import llm

    model = options.get("model") or "openai/gpt-oss-120b"
    wrong = faults.on()
    how = wrong.get("builder") if _asking() == "itinerary_builder" else None
    if how is None and model == llm.GROQ_SMALL_MODEL:
        how = wrong.get("groq_small")
    return faults.real_groq(how, options) if how else StandInChatModel(model=model)


def gemini(**options: Any) -> BaseChatModel:
    """What `ChatGoogleGenerativeAI(model=…, …)` makes while the stand-ins are in — or, with a fault on, the real one."""
    how = faults.on().get("gemini")
    return (
        faults.real_gemini(how, options) if how else StandInChatModel(model=options.get("model") or "gemini-3.5-flash")
    )


# ── The embedding model ────────────────────────────────────────────────────


def word_vector(text: str) -> list[float]:
    """A stand-in embedding: a dimension per word, so two texts are as close as the words they share.

    Two trips to the same place for the same interests come out alike, as they do with the real
    model — so GET /similar finds a traveller's other trips, and has something to return.
    """
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for word in re.findall(r"[a-z]+", text.lower()):
        vector[zlib.crc32(word.encode()) % EMBEDDING_DIMENSIONS] += 1.0
    if not any(vector):
        vector[-1] = 1.0  # no words at all: still a direction (a zero vector has no cosine)
    return vector


async def embed(text: str, task_type: str = "", timeout: float = 0.0) -> list[float]:
    """In place of `embedder._embed_once`: the one function that reaches the embedding API."""
    await asyncio.sleep(timing.EMBEDDING.seconds())
    return word_vector(text)
