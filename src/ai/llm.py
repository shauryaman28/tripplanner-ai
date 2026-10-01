"""Shared LLM plumbing: model IDs and tolerant JSON parsing of model replies."""

from __future__ import annotations

import json
from typing import Any

try:
    from app.core.config import settings
except ImportError:
    from src.backend.app.core.config import settings

GEMINI_MODEL = settings.GEMINI_MODEL
GROQ_MODEL = settings.GROQ_MODEL


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
