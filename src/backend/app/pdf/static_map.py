"""A static map image for the PDF, drawn from map tiles.

The roadmap names the Google Maps Static API. This draws the picture from the
same tiles the web map uses instead (DECISIONS #101): no key and no billing
account, and the pins are the web map's own — coloured by day, numbered in
visiting order, the hotel in gold.

    tiles (httpx, cached in Redis) → stitched and toned down → routes and pins → JPEG

Any failure — a tile that will not download, a body that is not an image, a
slow server — raises StaticMapError, and the caller leaves the map out.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import math
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from app.pdf.theme import (
    BED_STROKE_WIDTH,
    BED_STROKES,
    CAP_HEIGHT,
    DIAMOND_SIDE,
    ICON_BOX,
    INK_500,
    PIN_CORNER,
    PLANE_FOLD,
    PLANE_OUTLINE,
    REGULAR,
    SEMIBOLD,
    WHITE,
    PinStyle,
    font_path,
)

logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and a hosted tile provider takes its key in the URL (see DECISIONS #70).
logging.getLogger("httpx").setLevel(logging.WARNING)

TILE_SIZE = 256
WIDTH, HEIGHT = 800, 540  # the view, in tile pixels
SCALE = 2  # the picture is twice that: tiles enlarged, pins drawn sharp — what a retina screen shows
# Pillow draws shapes with hard edges, so every pin and route is drawn this much larger and reduced.
PIN_SMOOTHING, ROUTE_SMOOTHING = 4, 2

MIN_ZOOM, MAX_ZOOM = 3, 14  # 14 is where the web map stops zooming in on a fit, too
SINGLE_POINT_ZOOM = 13
PADDING = 48  # clear space around the framed points, as on the web map
MAX_LATITUDE = 85.0511  # where the Web Mercator square ends

PIN_SIZE = 26  # in view pixels; the web map's pin is 30 on a smaller picture
PIN_RING = 2
PIN_SPACING = 0.85  # pins closer than this share of their size are nudged apart (see spread)
ROUTE_WIDTH, ROUTE_CASING = 3.5, 7.0
BACKGROUND = "#eceeea"

# OpenStreetMap's tile usage policy (operations.osmfoundation.org/policies/tiles): a User-Agent
# that names the application, tiles kept for at least seven days, and only the tiles of the view
# being drawn — no fetching ahead. It sets no connection limit; four is fewer than a browser's six.
USER_AGENT = "tripplanner-ai (github.com/shauryaman28/tripplanner-ai)"
TILE_TTL = 7 * 24 * 3600
DOWNLOADS_AT_ONCE = 4
TILE_TIMEOUT = 6.0  # seconds, per tile
RENDER_TIMEOUT = 12.0  # seconds, for the whole map — the export must answer well inside the proxy's 30

# Maps are drawn one at a time: the fonts and the cached pin pictures are shared between the
# worker threads, and a map takes some tens of milliseconds.
_COMPOSING = threading.Lock()


class StaticMapError(Exception):
    """The map could not be drawn. Never carries a tile URL: it may hold the provider's key."""


@dataclass(frozen=True)
class MapPin:
    lat: float
    lng: float
    style: PinStyle
    label: str = ""
    icon: str | None = None  # "bed" | "plane", instead of a label


@dataclass(frozen=True)
class MapRoute:
    color: str
    points: tuple[tuple[float, float], ...]  # (lat, lng), in visiting order


@dataclass(frozen=True)
class View:
    zoom: int
    left: float  # world-pixel position of the picture's top-left corner
    top: float


# ── Where things are ───────────────────────────────────────────────────────


def project(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """Latitude / longitude → pixels on the Web Mercator world at this zoom."""
    size = TILE_SIZE * 2**zoom
    sine = math.sin(math.radians(max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))))
    return (lng + 180.0) / 360.0 * size, (0.5 - math.log((1 + sine) / (1 - sine)) / (4 * math.pi)) * size


def choose_view(points: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> View:
    """The closest zoom at which every point fits inside the padded picture, centred on them."""
    if not points:
        raise StaticMapError("there is nothing to put on the map")

    zoom = SINGLE_POINT_ZOOM if len(points) == 1 else MAX_ZOOM
    while True:
        xs, ys = zip(*(project(lat, lng, zoom) for lat, lng in points))
        fits = max(xs) - min(xs) <= WIDTH - 2 * PADDING and max(ys) - min(ys) <= HEIGHT - 2 * PADDING
        if fits or zoom == MIN_ZOOM:
            break
        zoom -= 1
    return View(zoom=zoom, left=(min(xs) + max(xs)) / 2 - WIDTH / 2, top=(min(ys) + max(ys)) / 2 - HEIGHT / 2)


def tile_grid(view: View) -> list[tuple[int, int]]:
    """(column, row) of every tile the picture touches. Rows off the top or bottom of the world are left out."""
    columns = range(math.floor(view.left / TILE_SIZE), math.floor((view.left + WIDTH - 1) / TILE_SIZE) + 1)
    rows = range(math.floor(view.top / TILE_SIZE), math.floor((view.top + HEIGHT - 1) / TILE_SIZE) + 1)
    return [(column, row) for row in rows for column in columns if 0 <= row < 2**view.zoom]


def tile_url(template: str, zoom: int, column: int, row: int) -> str:
    """Fill a Leaflet-style template. The world repeats sideways, so a column wraps round."""
    column %= 2**zoom
    filled = template.replace("{s}", "abc"[(column + row) % 3]).replace("{r}", "")
    return filled.replace("{z}", str(zoom)).replace("{x}", str(column)).replace("{y}", str(row))


def spread(points: list[tuple[float, float]], distance: float, rounds: int = 32) -> list[tuple[float, float]]:
    """Nudge pins that would hide each other apart, by as little as it takes.

    A hotel beside the day's first stop is the rule, not the exception, and on
    a still picture there is no zooming in to tell them apart. Each pair closer
    than `distance` is pushed apart along the line between them; pins on the
    very same spot fan out, in an order that depends only on their position in
    the list — so the same plan always draws the same map.
    """
    placed = [list(point) for point in points]
    for _ in range(rounds):
        moved = False
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                dx, dy = placed[j][0] - placed[i][0], placed[j][1] - placed[i][1]
                gap = math.hypot(dx, dy)
                if gap >= distance - 0.01:
                    continue
                if gap < 0.01:  # no direction to push along: pick one from the golden angle
                    angle = 2.399963 * (i + j)
                    dx, dy, gap = math.cos(angle), math.sin(angle), 1.0
                push = (distance - math.hypot(placed[j][0] - placed[i][0], placed[j][1] - placed[i][1])) / 2
                placed[i][0] -= dx / gap * push
                placed[i][1] -= dy / gap * push
                placed[j][0] += dx / gap * push
                placed[j][1] += dy / gap * push
                moved = True
        if not moved:
            break
    return [(x, y) for x, y in placed]


# ── Tiles ──────────────────────────────────────────────────────────────────


async def _download_tile(client: httpx.AsyncClient, url: str) -> bytes:
    """One tile over the network — the seam the tests replace."""
    response = await client.get(url)
    response.raise_for_status()
    return response.content


def _check_tile(data: bytes) -> None:
    """Raise unless these bytes are a picture."""
    with Image.open(io.BytesIO(data)) as tile:
        tile.verify()


async def _load_tiles(template: str, view: View, cache: Any | None) -> dict[tuple[int, int], bytes]:
    """Every tile of the view, from the cache where it has them. Raises on the first one that cannot be had."""
    provider = hashlib.sha1(template.encode()).hexdigest()[:8]  # another tile server is another set of pictures
    limit = asyncio.Semaphore(DOWNLOADS_AT_ONCE)
    fresh: dict[str, bytes] = {}

    async def load(client: httpx.AsyncClient, column: int, row: int) -> bytes:
        key = f"maptile:{provider}:{view.zoom}/{column % 2**view.zoom}/{row}"
        if cache is not None:
            try:
                if cached := await cache.get(key):
                    data = base64.b64decode(cached)
                    _check_tile(data)
                    return data
            except Exception:  # the cache is away, or holds something that is not a tile: fetch it instead
                logger.debug("Tile cache read failed for %s", key, exc_info=True)

        url = tile_url(template, view.zoom, column, row)
        async with limit:
            try:
                data = await _download_tile(client, url)
            except httpx.TransportError:  # a dropped connection is worth one more try; an HTTP error is an answer
                data = await _download_tile(client, url)
        _check_tile(data)
        fresh[key] = data
        return data

    grid = tile_grid(view)
    async with httpx.AsyncClient(
        timeout=TILE_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        loading = [asyncio.ensure_future(load(client, column, row)) for column, row in grid]
        try:
            tiles = await asyncio.gather(*loading)
        finally:  # one tile failed, or the whole map timed out: stop asking for the rest
            for task in loading:
                task.cancel()
            await asyncio.gather(*loading, return_exceptions=True)

    if cache is not None:  # only once the whole set is in hand
        for key, data in fresh.items():
            try:
                await cache.set(key, base64.b64encode(data).decode(), ex=TILE_TTL)
            except Exception:
                logger.debug("Tile cache write failed for %s", key, exc_info=True)
                break  # the cache is away; the map is still fine

    return dict(zip(grid, tiles))


# ── Drawing ────────────────────────────────────────────────────────────────
#
# Each pin and each route is drawn on a small picture of its own, larger than it ends up, and
# reduced onto the map: that is what gives it soft edges. Drawing them all on one full-size layer
# and reducing that looks the same but took 85 MB more memory for every map.


def _quieten(image: Image.Image) -> Image.Image:
    """Tone the basemap down so the pins carry the colour — the web map's CSS filter, in the same order."""
    image = ImageEnhance.Color(image).enhance(0.55)
    image = ImageEnhance.Contrast(image).enhance(0.92)
    return ImageEnhance.Brightness(image).enhance(1.05)


@lru_cache(maxsize=8)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path(name)), size)


def _stroke(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], colour: str | int, width: int) -> None:
    """A line with round joins and round ends (Pillow's own lines end square)."""
    draw.line(points, fill=colour, width=width, joint="curve")
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=colour)


def _draw_icon(draw: ImageDraw.ImageDraw, x: float, y: float, style: PinStyle, icon: str, box: float) -> None:
    """The bed or the paper plane, scaled from its 24 × 24 drawing into a box centred on (x, y)."""
    scale = box / ICON_BOX

    def place(points: tuple[tuple[float, float], ...]) -> list[tuple[float, float]]:
        return [(x + (px - ICON_BOX / 2) * scale, y + (py - ICON_BOX / 2) * scale) for px, py in points]

    if icon == "bed":
        for line in BED_STROKES:
            _stroke(draw, place(line), style.ink, max(round(BED_STROKE_WIDTH * scale), 1))
    else:
        draw.polygon(place(PLANE_OUTLINE), fill=style.ink)
        draw.line(place(PLANE_FOLD), fill=style.color, width=max(round(1.3 * scale), 1))


@lru_cache(maxsize=128)
def _pin_picture(style: PinStyle, label: str, icon: str | None) -> Image.Image:
    """One pin at the size it has on the finished map: a white ring, the fill, then its number or icon."""
    unit = SCALE * PIN_SMOOTHING
    size = PIN_SIZE * unit
    side = size * (DIAMOND_SIDE if style.shape == "diamond" else 1.0)
    ring = PIN_RING * unit
    canvas = math.ceil(size * 1.2 / PIN_SMOOTHING) * PIN_SMOOTHING  # room for a diamond's corners
    middle = canvas / 2

    mark = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    edge = (middle - side / 2, middle - side / 2, middle + side / 2 - 1, middle + side / 2 - 1)
    draw.rounded_rectangle(edge, radius=side * PIN_CORNER[style.shape], fill=WHITE)
    draw.rounded_rectangle(
        (edge[0] + ring, edge[1] + ring, edge[2] - ring, edge[3] - ring),
        radius=max(side * PIN_CORNER[style.shape] - ring, 0),
        fill=style.color,
    )
    if style.shape == "diamond":
        mark = mark.rotate(45, resample=Image.Resampling.BICUBIC)
        draw = ImageDraw.Draw(mark)

    if icon:
        _draw_icon(draw, middle, middle, style, icon, size * 0.54)
    elif label:
        font = _font(SEMIBOLD, round(size * 0.48))
        draw.text((middle, middle + font.size * CAP_HEIGHT / 2), label, font=font, fill=style.ink, anchor="ms")
    return mark.resize((canvas // PIN_SMOOTHING,) * 2, Image.Resampling.LANCZOS)


def _draw_pin(picture: Image.Image, x: float, y: float, pin: MapPin) -> None:
    """Put a pin on the map, centred on (x, y). One that lies off the picture is simply not seen."""
    mark = _pin_picture(pin.style, pin.label, pin.icon)
    picture.paste(mark, (round(x - mark.width / 2), round(y - mark.height / 2)), mark)


def _draw_line(picture: Image.Image, points: list[tuple[float, float]], colour: str, width: float) -> None:
    """One line with soft edges: drawn larger on a mask that covers just the line, reduced, painted through it."""
    xs, ys = zip(*points)
    left, top = max(math.floor(min(xs) - width), 0), max(math.floor(min(ys) - width), 0)
    right = min(math.ceil(max(xs) + width), picture.width)
    bottom = min(math.ceil(max(ys) + width), picture.height)
    if right <= left or bottom <= top:
        return  # the whole line is off the picture

    mask = Image.new("L", ((right - left) * ROUTE_SMOOTHING, (bottom - top) * ROUTE_SMOOTHING), 0)
    on_mask = [((x - left) * ROUTE_SMOOTHING, (y - top) * ROUTE_SMOOTHING) for x, y in points]
    _stroke(ImageDraw.Draw(mask), on_mask, 255, round(width * ROUTE_SMOOTHING))
    picture.paste(colour, (left, top), mask.resize((right - left, bottom - top), Image.Resampling.LANCZOS))


def _draw_routes(picture: Image.Image, routes: list[MapRoute], view: View) -> None:
    """Each day's line in its colour over a white casing, which lifts it off the basemap."""
    lines = []
    for route in routes:
        world = (project(lat, lng, view.zoom) for lat, lng in route.points)
        lines.append((route.color, [((px - view.left) * SCALE, (py - view.top) * SCALE) for px, py in world]))

    for _, points in lines:  # every casing first, so one day's white edge never cuts another day's line
        _draw_line(picture, points, WHITE, ROUTE_CASING * SCALE)
    for colour, points in lines:
        _draw_line(picture, points, colour, ROUTE_WIDTH * SCALE)


def _draw_attribution(image: Image.Image, text: str) -> None:
    """The tile provider's credit, bottom right — the licence asks for it wherever the map is shown."""
    if not text:
        return
    draw = ImageDraw.Draw(image, "RGBA")
    font = _font(REGULAR, 10 * SCALE)
    pad_x, pad_y = 5 * SCALE, 3 * SCALE
    width = draw.textlength(text, font=font)
    box = (image.width - width - 2 * pad_x, image.height - font.size - 2 * pad_y, image.width, image.height)
    draw.rounded_rectangle(box, radius=4 * SCALE, fill=(255, 255, 255, 215), corners=(True, False, False, False))
    draw.text((box[0] + pad_x, image.height - pad_y), text, font=font, fill=INK_500, anchor="ls")


def _compose(
    tiles: dict[tuple[int, int], bytes], view: View, pins: list[MapPin], routes: list[MapRoute], attribution: str
) -> bytes:
    """Tiles + routes + pins → JPEG bytes. Runs in a worker thread: it is all CPU."""
    with _COMPOSING:
        base = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
        for (column, row), data in tiles.items():
            with Image.open(io.BytesIO(data)) as tile:
                tile = tile.convert("RGB")
                if tile.size != (TILE_SIZE, TILE_SIZE):  # a provider serving larger ("@2x") tiles
                    tile = tile.resize((TILE_SIZE, TILE_SIZE), Image.Resampling.LANCZOS)
                base.paste(tile, (round(column * TILE_SIZE - view.left), round(row * TILE_SIZE - view.top)))
        picture = _quieten(base).resize((WIDTH * SCALE, HEIGHT * SCALE), Image.Resampling.LANCZOS)

        _draw_routes(picture, routes, view)
        world = (project(pin.lat, pin.lng, view.zoom) for pin in pins)
        centres = spread([(px - view.left, py - view.top) for px, py in world], PIN_SIZE * PIN_SPACING)
        for pin, (x, y) in zip(pins, centres):  # in the order given: later pins sit on top
            _draw_pin(picture, x * SCALE, y * SCALE, pin)
        _draw_attribution(picture, attribution)

        out = io.BytesIO()
        # no chroma subsampling: the pins' coloured edges stay crisp
        picture.save(out, "JPEG", quality=86, subsampling=0, optimize=True)
        return out.getvalue()


# ── The map ────────────────────────────────────────────────────────────────


async def render_static_map(
    pins: list[MapPin],
    routes: list[MapRoute],
    frame: tuple[tuple[float, float], ...] | list[tuple[float, float]],
    *,
    tile_url_template: str,
    attribution: str = "",
    cache: Any | None = None,
) -> bytes:
    """A JPEG of the map framing `frame`, with the routes and pins on it.

    `cache` is anything with Redis's async `get(key)` / `set(key, value, ex=)`;
    tiles are kept there for a week. Raises StaticMapError when the map cannot
    be drawn — the message never contains a tile URL.
    """
    if not tile_url_template:
        raise StaticMapError("no tile server is configured (MAP_TILE_URL)")
    view = choose_view(frame)

    try:
        tiles = await asyncio.wait_for(_load_tiles(tile_url_template, view, cache), timeout=RENDER_TIMEOUT)
        return await asyncio.to_thread(_compose, tiles, view, pins, routes, attribution)
    except httpx.HTTPStatusError as exc:
        raise StaticMapError(f"the tile server answered HTTP {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise StaticMapError(f"the tile server could not be reached ({type(exc).__name__})") from None
    except asyncio.TimeoutError:
        raise StaticMapError(f"the tiles took longer than {RENDER_TIMEOUT:.0f} s") from None
    except Exception as exc:  # e.g. a body that is not an image
        raise StaticMapError(f"the map could not be drawn ({type(exc).__name__})") from exc
