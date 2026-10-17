"""The headers every answer of the API carries, whatever it is (Phase 35).

    X-Content-Type-Options: nosniff           a browser takes the Content-Type as said: JSON is never run as script
    X-Frame-Options: DENY                     no page of this API in anybody's frame
    Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
                                              the API serves data, not pages: nothing it answers may load anything
    Referrer-Policy: no-referrer              a link followed from an answer says nothing of where it came from
    Permissions-Policy: camera=(), …          and asks for nothing
    Strict-Transport-Security                 in production only, where the platform serves https: a year

The API docs (`/docs`, `/redoc`) are pages, and load Swagger UI and ReDoc from a CDN with an inline
script to start them: they get a policy that allows exactly that, and still no frame.

An answer that already says one of these keeps its own. They are added by a plain ASGI layer (an
event stream must not be wrapped, as with the other layers of main.py) — and, because Starlette
answers an unhandled error outside every layer an app adds, by the handler that writes the 500 too.
"""

from __future__ import annotations

from collections.abc import Iterable

API_POLICY = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# What FastAPI's two documentation pages load: Swagger UI and ReDoc from jsDelivr, an inline script to start
# them, FastAPI's favicon, ReDoc's fonts from Google and its web worker; the schema from this server.
DOCS_POLICY = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "connect-src 'self'; "
    "worker-src blob:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})
HSTS = "max-age=31536000; includeSubDomains"


def security_headers(path: str, production: bool) -> dict[str, str]:
    """The headers an answer to `path` carries."""
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": DOCS_POLICY if path in DOCS_PATHS else API_POLICY,
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }
    if production:
        headers["Strict-Transport-Security"] = HSTS
    return headers


def merged(existing: Iterable[tuple[bytes, bytes]], path: str, production: bool) -> list[tuple[bytes, bytes]]:
    """`existing` (an ASGI response's headers) with every security header it does not already have."""
    out = list(existing)
    have = {name.lower() for name, _ in out}
    for name, value in security_headers(path, production).items():
        if name.lower().encode() not in have:
            out.append((name.lower().encode(), value.encode()))
    return out


class SecurityHeaders:
    """Plain ASGI: every HTTP answer gets the headers above, as it starts. `production()` is asked each time."""

    def __init__(self, app, production) -> None:
        self.app, self.production = app, production

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = merged(message.get("headers") or [], path, self.production())
            await send(message)

        await self.app(scope, receive, send_with_headers)
