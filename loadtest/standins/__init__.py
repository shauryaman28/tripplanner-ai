"""Stand-ins for the models and the providers, for the load test (Phase 30).

A planning request asks three models and five providers, and none of them can take load: Gemini's
free tier is about twenty requests a day, Groq allows one plan's worth of tokens every few seconds,
the flight and hotel providers are sandboxes. So while the load test runs, these answer instead —
and everything else is the real thing: the app, Postgres through its pooler, Redis, the tool server
as a process of its own with its threads, its cache, its rate limits and its backoff.

    models.py       the chat models and the embedding model. They answer through LangChain, so a
                    call is still counted, metered and a span.
    providers.py    the five providers, as the tools reach them: `httpx.get` and `httpx.post`.
    timing.py       how long each of them takes. A stand-in that answers at once would move the
                    bottleneck to wherever the fakes are not: each one waits as long as the real
                    thing was measured to take.
    seams.py        the four names the stand-ins are put behind.
    app.py          what the web server is told to serve in place of `app.main:app`: it puts the
                    stand-ins in, and then is that app.
    tool_server.py  the same for the tool server's process.
    guard.py        why this cannot be a deployment.

**There is no switch.** No setting, flag or environment variable turns stand-ins on — one of those
gets set by accident. They are a different program:

- they live outside `src/`, and the image that is deployed copies `src/`, the migrations and nothing
  else: the code is not there to be switched on;
- the load test's image is that image with this directory added (docker/loadtest/Dockerfile) and a
  different thing to serve — `loadtest.standins.app:app`;
- and that program refuses to start against a database or a Redis that is not on this machine
  (guard.py), so the load test's image handed a deployment's settings by mistake does not start.

Everything a stand-in makes says so: the hotels are "Stand-in …", the places "… (stand-in)".
"""
