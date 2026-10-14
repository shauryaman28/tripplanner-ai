"""Which model is to go wrong, and how (Phase 31) — for as long as a file says so.

    {"gemini": "429"}                          the short-prompt model's provider says "quota exceeded"
    {"gemini": "429", "groq_small": "429"}     …and so does the one that is asked instead
    {"builder": "hang"}                        the model that writes the itinerary never answers
    {"embedding": "500"}                       the embedding API fails every request (Phase 32)

Who: `gemini` (intent parsing, the refinement classifier), `groq_small` (what those fall back to),
`builder` (the large model, when the itinerary builder is the one asking), `embedding`. How: `429`,
`500`, `hang`.

With a fault on, that model is not a stand-in but the provider's own client — made with exactly
the options the app made it with — pointed at fault_server.py. What happens next is then the
client's doing and the app's, which is what is being asked.

The embedding API is the exception (Phase 32): what stands in for it is the one function that
reaches it, so there is no client of the provider's to point anywhere, and its fault is the
stand-in itself failing. That is enough for what it is used to ask — whether a job that could not
embed is retried after a restart — and says nothing about the embedding client's own retries.

The faults are a file inside the load test's container (scripts/chaos.py writes and removes it),
read each time a model is made: a fault can begin and end while the stack runs, and needs nothing
that an exercise might itself have broken — not Redis, not the database. Like everything here it is
no part of the app: there is nothing in `src/` that reads it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loadtest.standins import guard

FILE = Path(os.environ.get("STANDIN_FAULTS_FILE", "/tmp/standin-faults.json"))
FAR_END = os.environ.get("STANDIN_FAULT_SERVER", "http://provider-faults:9099").rstrip("/")

WHO = ("gemini", "groq_small", "builder", "embedding")
HOW = ("429", "500", "hang")


def on() -> dict[str, str]:
    """The faults that are on right now: {who: how}. No file, or one that cannot be read, is none."""
    try:
        told = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(told, dict):
        return {}
    return {who: how for who, how in told.items() if who in WHO and how in HOW}


class EmbeddingFailed(RuntimeError):
    """The embedding API's stand-in, failing because a fault says to."""


def _far_end(how: str) -> str:
    # The real client sends its key with every request. The stack's keys are placeholders — and the
    # address is still held to this machine, like the database and the Redis are.
    guard.check(STANDIN_FAULT_SERVER=FAR_END)
    return f"{FAR_END}/{how}"


def real_gemini(how: str, options: dict[str, Any]):
    """Google's own chat client, as the app asked for it, talking to a server that fails."""
    from langchain_google_genai.chat_models import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(**{**options, "base_url": _far_end(how)})


def real_groq(how: str, options: dict[str, Any]):
    """Groq's own chat client, as the app asked for it, talking to a server that fails."""
    from langchain_groq.chat_models import ChatGroq  # the class itself: seams.py replaces the package's name for it

    return ChatGroq(**{**options, "base_url": _far_end(how)})
