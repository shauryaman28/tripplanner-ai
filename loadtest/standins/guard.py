"""Why the stand-ins cannot be a deployment: they refuse a database or a Redis that is not on this machine.

Two things already keep them out of production — they are not in the image that is deployed, and
nothing in the app can switch them on. This is the third, for the one accident those two leave:
somebody takes the load test's own image and gives it a deployment's settings. A deployment's
database and Redis are somewhere on the internet, and an address on the internet has a dot in it
(`ep-cool-name-pooler.ap-southeast-1.aws.neon.tech`, `apt-mako-12345.upstash.io`); the load test's
are other containers on this machine, called by one word (`pooler`, `upstash-standin`). So: a host
with a dot in its name, and the program does not start.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Names with a dot in them that are still this machine.
THIS_MACHINE = frozenset({"127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"})


class NotTheLoadTest(RuntimeError):
    """The stand-ins were started against something that is not the load test's own stack."""


def is_this_machine(url: str) -> bool:
    """Whether a URL's host is this machine, or a container beside it: one word, or a loopback address."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and (host in THIS_MACHINE or "." not in host)


def check(**urls: str) -> None:
    """Raise unless every URL given is on this machine. `urls`: the setting's name → its value."""
    elsewhere = sorted(name for name, url in urls.items() if not is_this_machine(url))
    if elsewhere:
        raise NotTheLoadTest(
            f"{' and '.join(elsewhere)} {'is' if len(elsewhere) == 1 else 'are'} not on this machine. "
            "This is the load test's program: its models and its flight, hotel and attraction searches are "
            "stand-ins that make plans up. It only runs against the load test's own database and Redis "
            "(python scripts/loadtest.py up) — the app itself is app.main:app."
        )
