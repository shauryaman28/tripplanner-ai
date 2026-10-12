"""Where the stand-ins go in: four names, in the process that serves the app.

    langchain_groq.ChatGroq                        the class the planner makes its Groq models of
    src.ai.llm.ChatGoogleGenerativeAI              …and its Gemini model
    src.ai.embeddings.embedder._embed_once         the one function that reaches the embedding API
    src.ai.mcp_client.client.StdioServerParameters how the tool server's process is started — it is
                                                   started through tool_server.py, which puts the
                                                   providers' stand-ins in before the server runs

Nothing in the app is changed for this, and nothing in the app reads a setting to decide it: the
four names are assigned to, by a program that is not the app (app.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from loadtest.standins import models

TOOL_SERVER = "loadtest.standins.tool_server"


def _targets() -> list[tuple[Any, str, Any]]:
    """(module, name, what it becomes) — looked up when asked, so that importing this module imports nothing of the app."""
    import langchain_groq
    from mcp import StdioServerParameters

    from src.ai import llm
    from src.ai.embeddings import embedder
    from src.ai.mcp_client import client

    def the_standin_tool_server(**parameters: Any) -> StdioServerParameters:
        """The tool server's process, started as the client starts it — but through the module that
        puts the providers' stand-ins in first."""
        return StdioServerParameters(**{**parameters, "args": ["-m", TOOL_SERVER]})

    return [
        (langchain_groq, "ChatGroq", models.groq),
        (llm, "ChatGoogleGenerativeAI", models.gemini),
        (embedder, "_embed_once", models.embed),
        (client, "StdioServerParameters", the_standin_tool_server),
    ]


def install() -> None:
    """Put the stand-ins in, in this process, for as long as it lives."""
    for module, name, stand_in in _targets():
        setattr(module, name, stand_in)


@contextmanager
def installed() -> Iterator[None]:
    """The same for the length of a block, and the real things back afterwards. For the tests."""
    targets = _targets()
    real = [getattr(module, name) for module, name, _ in targets]
    try:
        for module, name, stand_in in targets:
            setattr(module, name, stand_in)
        yield
    finally:
        for (module, name, _), was in zip(targets, real):
            setattr(module, name, was)
