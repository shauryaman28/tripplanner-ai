"""The five providers' stand-ins: what `httpx.get` and `httpx.post` are, inside the tool server.

The tools are left as they are (src/ai/mcp_server/tools.py): they validate, look in their cache,
wait for their rate limit, send a request, read the answer, cut it to the traveller's budget. Only
the request itself is answered here — in the shape each provider answers in, after as long as that
provider takes (timing.py), on the worker thread that made it, as a real request would hold it.

So what a load test loads is real: the tool server's threads, its Redis cache and the keys in it,
one search per key at a time, and the rate limits — Duffel's thirty flight searches a minute are
kept by the app's own limiter, and a load test that asks for more distinct flights than that finds
out what a traveller would be told.

A request to anywhere else is refused. Nothing leaves the machine.
"""

from __future__ import annotations

import time
import zlib
from datetime import date
from typing import Any

import httpx

from loadtest.standins import timing

HOSTS = {
    "api.duffel.com": "duffel",
    "api.liteapi.travel": "liteapi",
    "api.opentripmap.com": "opentripmap",
    "api.openweathermap.org": "openweather",
    "nominatim.openstreetmap.org": "nominatim",
}

# Where the load test's travellers go: (latitude, longitude, airport code, airport). Every one is a
# city the flight tool knows an airport for. A destination that is not here is still answered — as
# a place somewhere in the middle of India.
PLACES: dict[str, tuple[float, float, str, str]] = {
    "goa": (15.2993, 74.1240, "GOI", "Goa International Airport"),
    "jaipur": (26.9124, 75.7873, "JAI", "Jaipur International Airport"),
    "kochi": (9.9312, 76.2673, "COK", "Cochin International Airport"),
    "udaipur": (24.5854, 73.7125, "UDR", "Maharana Pratap Airport"),
    "varanasi": (25.3176, 82.9739, "VNS", "Lal Bahadur Shastri Airport"),
    "amritsar": (31.6340, 74.8723, "ATQ", "Sri Guru Ram Dass Jee International Airport"),
    "leh": (34.1526, 77.5771, "IXL", "Kushok Bakula Rimpochee Airport"),
    "srinagar": (34.0837, 74.7973, "SXR", "Srinagar International Airport"),
    "jodhpur": (26.2389, 73.0243, "JDH", "Jodhpur Airport"),
    "agra": (27.1767, 78.0081, "AGR", "Agra Airport"),
    "madurai": (9.9252, 78.1198, "IXM", "Madurai Airport"),
    "bhubaneswar": (20.2961, 85.8245, "BBI", "Biju Patnaik International Airport"),
}
SOMEWHERE = (22.9734, 78.6569, "NAG", "an airport")
DELHI = (28.5562, 77.1000, "DEL", "Indira Gandhi International Airport")
AIRPORTS = {code: (lat, lon, code, name) for lat, lon, code, name in [*PLACES.values(), DELHI]}

CARRIERS = (("6E", "9001"), ("AI", "9002"), ("UK", "9003"), ("SG", "9004"))
HOTELS = (  # (name, stars, review score, rupees a room a night)
    ("Stand-in Residency", 3, 7.8, 2400.0),
    ("Stand-in Courtyard", 3, 8.1, 3100.0),
    ("Stand-in Heritage House", 4, 8.6, 3900.0),
    ("Stand-in Lake View", 4, 8.4, 4800.0),
    ("Stand-in Palace", 5, 9.0, 6200.0),
    ("Stand-in Grand", 5, 9.2, 8500.0),
)

# What the attractions provider has, by the kind it is asked for: (name, the place's own kinds, popularity).
# Shaped like the real answers — plenty of well-known history, restaurants that are almost all rated 1
# (so a search for food fills up with lesser-known places in a second request, as the real one does).
_SIGHTS = (("Fort", 7), ("Palace", 7), ("Old City Gate", 3), ("Stepwell", 3), ("Haveli", 2), ("Clock Tower", 2))
BY_KIND: dict[str, tuple[tuple[str, str, int], ...]] = {
    "historic": tuple((name, "historic,fortifications,cultural,interesting_places", rate) for name, rate in _SIGHTS),
    "museums": (
        ("City Museum", "museums,cultural,interesting_places", 3),
        ("Crafts Museum", "museums,cultural", 2),
        ("Folk Art Museum", "museums,cultural", 2),
    ),
    "cultural": (
        ("Heritage Walk", "cultural,interesting_places", 2),
        ("Artists' Quarter", "cultural", 2),
        ("Music Hall", "cultural", 2),
        ("Folk Theatre", "cultural", 1),
    ),
    "foods": (("Spice Market", "foods,shops", 2), ("Thali House", "restaurants,foods", 1)),
    "restaurants": (("Rooftop Kitchen", "restaurants,foods", 1), ("Street Food Lane", "restaurants,foods", 1)),
    "cafes": (("Chai Corner", "cafes,foods", 1), ("Lassi Stall", "cafes,foods", 1)),
    "beaches": (
        ("North Beach", "beaches,natural", 3),
        ("Sunset Beach", "beaches,natural", 2),
        ("Fishermen's Cove", "beaches,natural", 2),
        ("Lighthouse Beach", "beaches,natural", 2),
        ("Coconut Grove Beach", "beaches,natural", 2),
    ),
    "natural": (
        ("Lakefront", "natural,water", 3),
        ("Hill Viewpoint", "natural,view_points", 2),
        ("River Ghat", "natural,water", 2),
    ),
    "national_parks": (
        ("Tiger Reserve", "national_parks,nature_reserves,natural", 3),
        ("Wildlife Sanctuary", "national_parks,nature_reserves,natural", 2),
    ),
    "nature_reserves": (("Bird Reserve", "nature_reserves,natural", 2),),
    "geological_formations": (("Rock Caves", "geological_formations,natural", 2),),
    "waterfalls": (("Falls", "waterfalls,natural", 3),),
    "religion": (("Old Temple", "religion,historic", 7), ("Riverside Shrine", "religion", 2)),
    "architecture": (("Arcade", "architecture,historic_architecture", 2),),
    "shops": (
        ("Old Market", "shops", 2),
        ("Textile Bazaar", "shops", 2),
        ("Silver Street", "shops", 2),
        ("Handloom House", "shops", 2),
        ("Pottery Lane", "shops", 2),
    ),
    "nightclubs": (("Night Bazaar", "nightclubs", 1),),
    "gardens_and_parks": (("Rose Garden", "gardens_and_parks", 2),),
    "water": (("Boat Jetty", "water", 2),),
    "view_points": (("Sunset Point", "view_points,natural", 2),),
    "interesting_places": tuple((name, "interesting_places,historic", rate) for name, rate in _SIGHTS),
}


def _varies(*parts: Any) -> int:
    """A number that is the same for the same search, and different for another: prices should not all be one price."""
    return zlib.crc32("|".join(map(str, parts)).encode())


def _place(query: str) -> tuple[float, float, str, str]:
    return PLACES.get(query.strip().lower(), SOMEWHERE)


def _nearest(lat: float, lon: float) -> str:
    """The destination a search around a point is about."""
    return min(PLACES, key=lambda name: (PLACES[name][0] - lat) ** 2 + (PLACES[name][1] - lon) ** 2).title()


# ── What each provider answers ─────────────────────────────────────────────


def flights(asked: dict) -> dict:
    """POST /air/offer_requests: four offers, cheapest first, priced for everyone travelling."""
    data = asked.get("data") or {}
    out = (data.get("slices") or [{}])[0]
    origin, destination, day = out.get("origin", "DEL"), out.get("destination", "GOI"), out.get("departure_date", "")
    travellers = max(1, len(data.get("passengers") or []))
    round_trip = len(data.get("slices") or []) > 1
    fare = 2600 + _varies(origin, destination) % 2200  # one way, one traveller

    def airport(code: str) -> dict:
        lat, lon, _, name = AIRPORTS.get(code, (*SOMEWHERE[:2], code, "an airport"))
        return {"iata_code": code, "name": name, "latitude": lat, "longitude": lon}

    offers = []
    for n, (carrier, number) in enumerate(CARRIERS):
        total = (fare + 450 * n) * travellers * (2 if round_trip else 1)
        segment = {
            "departing_at": f"{day}T{6 + 3 * n:02d}:10:00",
            "arriving_at": f"{day}T{8 + 3 * n:02d}:25:00",
            "marketing_carrier": {"iata_code": carrier},
            "marketing_carrier_flight_number": number,
            "origin": airport(origin),
            "destination": airport(destination),
        }
        offers.append(
            {
                "total_amount": f"{total:.2f}",
                "total_currency": "INR",
                "slices": [{"duration": "PT2H15M", "segments": [segment]}],
            }
        )
    return {"data": {"offers": offers}}


def hotels(asked: dict) -> dict:
    """POST /hotels/rates: six hotels, each priced for the whole stay and every room."""
    try:
        nights = max(1, (date.fromisoformat(asked["checkout"]) - date.fromisoformat(asked["checkin"])).days)
    except (KeyError, ValueError):
        nights = 1
    rooms = max(1, len(asked.get("occupancies") or []))
    if "cityName" in asked:
        city = str(asked["cityName"]).strip().title()
    else:
        city = _nearest(float(asked.get("latitude", SOMEWHERE[0])), float(asked.get("longitude", SOMEWHERE[1])))
    lat, lon, _, _ = _place(city)
    return {
        "hotels": [
            {
                "id": f"standin-{n}",
                "name": f"{name}, {city}",
                "stars": stars,
                "rating": rating,
                "address": f"{n + 1} Station Road",
                "city_name": city,
                "latitude": round(lat + 0.004 * n, 5),
                "longitude": round(lon - 0.003 * n, 5),
            }
            for n, (name, stars, rating, _) in enumerate(HOTELS)
        ],
        "data": [
            {
                "hotelId": f"standin-{n}",
                "roomTypes": [{"offerRetailRate": {"amount": nightly * nights * rooms, "currency": "INR"}}],
            }
            for n, (_, _, _, nightly) in enumerate(HOTELS)
        ],
    }


def somewhere(params: dict) -> list[dict]:
    """GET /search (Nominatim): the place, in India, well known."""
    lat, lon, _, _ = _place(str(params.get("q", "")))
    return [
        {
            "class": "boundary",
            "importance": 0.7,
            "lat": str(lat),
            "lon": str(lon),
            "address": {"country_code": "in", "country": "India"},
        }
    ]


def places(params: dict) -> list[dict]:
    """GET /places/radius (OpenTripMap): the places of the kinds asked for, at least as well known as asked."""
    lat, lon = float(params.get("lat", SOMEWHERE[0])), float(params.get("lon", SOMEWHERE[1]))
    least, limit = int(params.get("rate", 1)), int(params.get("limit", 10))
    city = _nearest(lat, lon)
    found: list[dict] = []
    for kind in str(params.get("kinds", "")).split(","):
        for n, (name, kinds, rate) in enumerate(BY_KIND.get(kind.strip(), ())):
            if rate % 4 >= least:  # 5 to 7 is the same scale again, for a heritage site
                found.append(
                    {
                        "name": f"{city} {name} (stand-in)",
                        "kinds": kinds,
                        "rate": rate,
                        "point": {
                            "lat": round(lat + 0.006 * (len(found) + 1), 5),
                            "lon": round(lon + 0.005 * (n + 1), 5),
                        },
                    }
                )
    return found[:limit]


def forecast(_params: dict) -> dict:
    """GET /forecast (OpenWeatherMap): one reading. Only a trip within five days is ever forecast."""
    return {
        "list": [{"dt": int(time.time()), "weather": [{"main": "Clear"}], "main": {"temp_max": 31, "temp_min": 22}}]
    }


# ── httpx.get / httpx.post ─────────────────────────────────────────────────


class NoSuchProvider(httpx.ConnectError):
    """A request to an address no stand-in answers for. It is not sent anywhere."""


def _answer(method: str, url: str, params: dict | None, body: dict | None) -> httpx.Response:
    provider = HOSTS.get(httpx.URL(url).host)
    request = httpx.Request(method, url)
    if provider is None:
        raise NoSuchProvider(f"the load test's stand-ins answer for five providers, and {request.url.host} is none")
    time.sleep(timing.PROVIDERS[provider].seconds())  # on the caller's thread, as a request would be
    answer: Any
    if provider == "duffel":
        answer = flights(body or {})
    elif provider == "liteapi":
        answer = hotels(body or {})
    elif provider == "nominatim":
        answer = somewhere(params or {})
    elif provider == "opentripmap":
        answer = places(params or {})
    else:
        answer = forecast(params or {})
    return httpx.Response(200, json=answer, request=request)


def get(url: str, *, params: dict | None = None, **_request: Any) -> httpx.Response:
    return _answer("GET", url, params, None)


def post(url: str, *, params: dict | None = None, json: dict | None = None, **_request: Any) -> httpx.Response:
    return _answer("POST", url, params, json)


class _Httpx:
    """What the tools' `httpx` is while the stand-ins are in: requests answered here, everything else the library's."""

    get = staticmethod(get)
    post = staticmethod(post)

    def __getattr__(self, name: str) -> Any:  # HTTPStatusError and the rest of what the tools name
        return getattr(httpx, name)


def install() -> None:
    """From now on in this process, the tools' requests are answered here. Run before the tool server starts."""
    from src.ai.mcp_server import tools

    tools.httpx = _Httpx()
