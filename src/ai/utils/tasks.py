"""Fire-and-forget asyncio tasks that cannot be garbage-collected mid-flight."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """asyncio.create_task, plus a strong reference until the task finishes.

    The event loop only keeps weak references to tasks, so an un-referenced
    background task can disappear before it completes.
    """
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
