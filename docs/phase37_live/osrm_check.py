"""Ten Goa journeys: OSRM's road distance and free-flow car time against schedule.estimate. One request a second."""

import json
import sys
import time

import httpx

sys.path.insert(0, "src/backend")
from src.ai import schedule

P = {
    "Fort Aguada": (15.4925, 73.7735),
    "Baga Beach": (15.5553, 73.7517),
    "Basilica of Bom Jesus": (15.5009, 73.9116),
    "Calangute": (15.5439, 73.7553),
    "Anjuna": (15.5733, 73.7407),
    "Panaji": (15.4909, 73.8278),
    "Dona Paula": (15.4561, 73.8037),
    "Colva": (15.2795, 73.9220),
    "Palolem": (15.0100, 74.0232),
    "Goa airport (GOI)": (15.3808, 73.8314),
}
PAIRS = [
    ("Fort Aguada", "Baga Beach"),
    ("Calangute", "Anjuna"),
    ("Panaji", "Dona Paula"),
    ("Baga Beach", "Basilica of Bom Jesus"),
    ("Goa airport (GOI)", "Fort Aguada"),
    ("Panaji", "Basilica of Bom Jesus"),
    ("Goa airport (GOI)", "Colva"),
    ("Colva", "Palolem"),
    ("Anjuna", "Panaji"),
    ("Goa airport (GOI)", "Calangute"),
]
rows = []
with httpx.Client(
    timeout=20, headers={"User-Agent": "tripplanner-ai phase37 estimate check (one-off, 10 requests)"}
) as http:
    for a, b in PAIRS:
        (la, lo), (lb, lob) = P[a], P[b]
        r = http.get(
            f"https://router.project-osrm.org/route/v1/driving/{lo},{la};{lob},{lb}", params={"overview": "false"}
        )
        route = r.json()["routes"][0]
        straight = schedule.straight_km(P[a], P[b])
        mode = schedule.mode_for(straight)
        est = schedule.estimate(straight, mode)
        rows.append(
            {
                "from": a,
                "to": b,
                "straight_km": round(straight, 1),
                "osrm_road_km": round(route["distance"] / 1000, 1),
                "osrm_car_min": round(route["duration"] / 60),
                "estimate_km": est["km"],
                "estimate_min": est["minutes"],
                "mode": mode,
            }
        )
        time.sleep(1.1)
for row in rows:
    print(json.dumps(row))
ratios = [r["osrm_road_km"] / r["straight_km"] for r in rows]
print("road/straight: mean %.2f, min %.2f, max %.2f" % (sum(ratios) / len(ratios), min(ratios), max(ratios)))
print("osrm car speed km/h:", [round(r["osrm_road_km"] / (r["osrm_car_min"] / 60), 1) for r in rows])
