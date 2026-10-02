"""Shared LLM plumbing: model IDs, the short-prompt call, tolerant JSON parsing of replies."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI

try:
    from app.core.config import settings
except ImportError:
    from src.backend.app.core.config import settings

logger = logging.getLogger(__name__)

GEMINI_MODEL = settings.GEMINI_MODEL
GROQ_MODEL = settings.GROQ_MODEL
GROQ_SMALL_MODEL = settings.GROQ_SMALL_MODEL

SHORT_PROMPT_TIMEOUT_S = 15


async def _ask_gemini(prompt: str) -> str:
    llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=0, max_retries=1, timeout=SHORT_PROMPT_TIMEOUT_S)
    return content_to_text((await llm.ainvoke(prompt)).content)


async def _ask_groq(prompt: str) -> str:
    from langchain_groq import ChatGroq

    # max_tokens leaves room for the model's reasoning before the (short) answer
    llm = ChatGroq(
        model=GROQ_SMALL_MODEL, temperature=0, max_tokens=1024, max_retries=1, timeout=SHORT_PROMPT_TIMEOUT_S
    )
    return content_to_text((await llm.ainvoke(prompt)).content)


async def ask(prompt: str) -> str:
    """Answer one short prompt (intent parsing, refinement classification). Raises if no model can.

    These calls sit on the request path, so they must be quick and must not
    depend on one provider. Gemini answers when it can — but its free tier
    allows 20 requests a day per model, and the SDK's default retries turned
    that 429 into ~36 s of waiting, longer than the frontend's proxy waits for
    POST /refine. So: one attempt, a short timeout, then Groq's small model.
    """
    try:
        return await _ask_gemini(prompt)
    except Exception as exc:
        if not settings.GROQ_API_KEY:
            raise
        logger.warning("Gemini unavailable (%s) — asking Groq %s instead", str(exc)[:120], GROQ_SMALL_MODEL)
        return await _ask_groq(prompt)


def content_to_text(content: Any) -> str:
    """LangChain chat models return a str, or a list of content blocks on newer models."""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in content or [])


def strip_fences(text: str) -> str:
    """Drop a ```json … ``` wrapper if the model added one."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json")
    return text.strip()


def parse_json_object(content: Any) -> dict:
    """Parse a model reply into a dict. Anything unparseable or non-object → {}."""
    try:
        parsed = json.loads(strip_fences(content_to_text(content)))
    except (ValueError, IndexError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
