"""A planned trip → the PDF file a browser downloads (Phase 19).

    plan.py        what the pages say — pure, mirrors the trip page
    static_map.py  the map picture, from tiles
    document.py    the pages (ReportLab), built from flowables.py

This module joins them: draw the map if it can be drawn, then lay the PDF out.
The map is the only part that needs the network, and the only part allowed to
be missing — a tile server that is down must not cost the traveller their PDF.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.pdf.document import build_pdf
from app.pdf.plan import MapFeatures, TripPlan, export_filename, map_features
from app.pdf.static_map import MapPin, MapRoute, StaticMapError, render_static_map
from app.pdf.theme import AIRPORT_STYLE, HOTEL_STYLE, day_style

logger = logging.getLogger(__name__)

MAP_INCLUDED = "included"
MAP_UNAVAILABLE = "unavailable"  # there were places to show, but the picture could not be drawn
MAP_NONE = "none"  # nothing in the plan has a location, or no tile server is configured


@dataclass(frozen=True)
class ExportedPdf:
    content: bytes
    filename: str  # in the destination's own script: "trip-गोवा-2027-12-10.pdf"
    ascii_filename: str  # the same in ASCII alone, for a client that cannot take the other: "trip-2027-12-10.pdf"
    map_status: str


def _pins_and_routes(features: MapFeatures) -> tuple[list[MapPin], list[MapRoute]]:
    """The web map's marks, bottom to top: airports, then the hotel, then the day's stops over both."""
    pins = [MapPin(lat=pin.lat, lng=pin.lng, style=AIRPORT_STYLE, icon="plane") for pin in features.airports]
    pins += [MapPin(lat=pin.lat, lng=pin.lng, style=HOTEL_STYLE, icon="bed") for pin in features.hotels]
    pins += [
        MapPin(lat=pin.lat, lng=pin.lng, style=day_style(pin.day), label=str(pin.order)) for pin in features.activities
    ]
    routes = [MapRoute(color=day_style(day).color, points=points) for day, points in features.routes]
    return pins, routes


async def export_itinerary(plan: TripPlan, *, cache: Any | None = None, trip_id: object = None) -> ExportedPdf:
    """Build the PDF for a plan. `cache` keeps map tiles between exports (the Redis client, or None)."""
    features = map_features(plan)
    image, map_status = None, MAP_NONE

    if features.framed and settings.MAP_TILE_URL:
        pins, routes = _pins_and_routes(features)
        try:
            image = await render_static_map(
                pins,
                routes,
                features.framed,
                tile_url_template=settings.MAP_TILE_URL,
                attribution=settings.MAP_ATTRIBUTION,
                cache=cache,
            )
            map_status = MAP_INCLUDED
        except StaticMapError as error:
            map_status = MAP_UNAVAILABLE
            logger.warning("PDF export for trip %s: the map was left out — %s", trip_id, error)
            logger.debug("Static map failure", exc_info=True)

    content = await asyncio.to_thread(build_pdf, plan, image)
    return ExportedPdf(
        content=content,
        filename=export_filename(plan.destination, plan.start_date),
        ascii_filename=export_filename(plan.destination, plan.start_date, ascii_only=True),
        map_status=map_status,
    )
