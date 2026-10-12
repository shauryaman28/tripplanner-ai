"""What the web server serves during the load test: the app, with the stand-ins in.

    uvicorn loadtest.standins.app:app …        (docker/loadtest/Dockerfile — every other word as in production)

Loading this module does three things, in this order:

1. checks that the database and the Redis are this machine's (guard.py) — or does not load;
2. puts the stand-ins where the models, the embedding call and the tool server's command are (seams.py);
3. imports the real app, and is it: the same routes, middleware, startup and shutdown.

It is a module the server imports, and not a script that starts the server, so that whatever
starts the server starts it the same way: a web server told to run several worker processes
imports its app in each of them, and each then has the stand-ins.
"""

from __future__ import annotations

import logging

from loadtest.standins import guard, seams

SAYS = "[STAND-INS]"  # scripts/loadtest.py looks for this line before it puts any load on a stack


def _load():
    from app.core.config import settings

    guard.check(DATABASE_URL=settings.DATABASE_URL, REDIS_URL=settings.REDIS_URL)
    seams.install()
    from app.main import app as the_app

    # After the app is imported: that is what sets the logging up, and this line should be one of the log's.
    logging.getLogger("loadtest.standins").warning(
        "%s the models, the embeddings and the five providers are stand-ins: every plan made here is made up", SAYS
    )
    return the_app


app = _load()
