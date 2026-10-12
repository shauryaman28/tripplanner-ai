"""The tool server's process during the load test: the real one, with the providers' stand-ins in.

    python -m loadtest.standins.tool_server        (started by the app — loadtest/standins/app.py)

It runs src/ai/mcp_server/server.py exactly as `python -m src.ai.mcp_server.server` does — its own
logging and tracing, its threads, its cache in Redis, its rate limits — after two things: the
check that this is the load test's stack (guard.py), and the tools' `httpx` made the stand-ins'
(providers.py).
"""

from __future__ import annotations

import runpy

from loadtest.standins import guard, providers

THE_REAL_ONE = "src.ai.mcp_server.server"


def main() -> None:
    from src.ai.mcp_server.config import mcp_settings

    guard.check(REDIS_URL=mcp_settings.REDIS_URL)
    providers.install()
    runpy.run_module(THE_REAL_ONE, run_name="__main__")


if __name__ == "__main__":
    main()
