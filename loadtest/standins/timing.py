"""How long each stand-in takes: as long as the real thing was measured to.

Measured on the production-like stack with real providers and models — the traces of Phase 29
(docs/phase29_build_log.md) and one more plan made for this phase (docs/loadtest/real-plan-jaipur.json):

                                Phase 29            Phase 30        here: fastest · usual · slowest
    intent parsing (Gemini)     about 2.4 s         4.6 s           1.8 · 2.4 · 4.6
    local tips (Groq)           1.3 s               2.5 s           1.0 · 1.4 · 2.6
    the itinerary (Groq)        8.4 s               5.3 s           5.0 · 6.0 · 8.4
      …of which written out     —                   1.8 s for 2,599 characters, in 35 events
    preferences (Groq)          1.0 s               0.9 s           0.7 · 0.95 · 1.5
    an embedding (Gemini)       —                   3.3 s, 0.9 s    0.8 · 1.0 · 3.3
    flights (Duffel)            6.9 s               3.4 s           3.4 · 5.5 · 7.0
    hotels (LiteAPI)            7.4 s               5.7 s           5.5 · 6.5 · 7.5
    geocoding (Nominatim)       2.1 s               2.2 s           1.0 · 2.1 · 2.2
    attractions (OpenTripMap)   2.1 · 0.7 · 0.7 s   1.2 · 0.8 · 0.8 0.7 · 0.8 · 2.1

Each wait is drawn between its fastest and its slowest, most often near the usual one. What is not
here: the 15 seconds a Gemini that does not answer costs before the fallback model is asked (seen
once in Phase 29), and a provider's own 429 — the load test measures the app, not a bad day at a
provider.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# 1.0: as measured. The tests set 0 — a stand-in is then still asked and still answers, at once.
scale = 1.0


@dataclass(frozen=True)
class Wait:
    fastest: float
    usual: float
    slowest: float

    def seconds(self) -> float:
        return random.triangular(self.fastest, self.slowest, self.usual) * scale


SHORT_PROMPT = Wait(1.8, 2.4, 4.6)  # intent parsing, the refinement classifier
LOCAL_TIPS = Wait(1.0, 1.4, 2.6)
ITINERARY = Wait(5.0, 6.0, 8.4)  # the whole reply; its last part arrives as it is written
PREFERENCES = Wait(0.7, 0.95, 1.5)
EMBEDDING = Wait(0.8, 1.0, 3.3)

# What a model writes in a second once it has started: 2,599 characters arrived in 1.78 s.
WRITING_CHARACTERS_A_SECOND = 1460.0
# …and how it arrives: a few characters a token, a few tokens to a packet.
PIECE_CHARACTERS = 16

PROVIDERS: dict[str, Wait] = {
    "duffel": Wait(3.4, 5.5, 7.0),
    "liteapi": Wait(5.5, 6.5, 7.5),
    "nominatim": Wait(1.0, 2.1, 2.2),
    "opentripmap": Wait(0.7, 0.8, 2.1),
    "openweather": Wait(0.3, 0.5, 0.9),  # not measured: no plan in either phase was near enough to need a forecast
}
