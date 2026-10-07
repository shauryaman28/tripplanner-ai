"""The itinerary vocabulary every part of the agent layer shares."""

from pydantic import BaseModel, Field

SLOTS = ("morning", "afternoon", "evening")

# The builder's only activity that is not a place: free time. It is never
# checked against the attraction list, never pinned on the map, and never
# counted as a top activity.
FREE_TIME = "Explore the area"

# ── Each traveller's share (Phase 25) ──────────────────────────────────────


class Share(BaseModel):
    name: str
    amount: int  # whole rupees; the shares of a trip add up to its total exactly


class PerPersonBreakdown(BaseModel):
    """A trip's cost split equally between its travellers — src/ai/pricing.per_person.

    Saved in an itinerary's `structured_data` under "per_person_breakdown", and
    returned by the `estimate_budget` tool, when more than one person travels.
    `flights`, `stay`, `activities` and `total` are one traveller's part.
    """

    travellers: int
    flights: float
    stay: float
    activities: float
    total: float
    shares: list[Share]


# ── Local intelligence (Phase 22) ──────────────────────────────────────────

# How much of it a plan carries. A model asked for "2 to 4 items" may write ten, or a paragraph
# where a sentence was asked for: these are the limits of what is kept.
MAX_TIPS = 5  # items in one list
MAX_BEST_TIMES = 6  # places in best_times
TIP_CHARS = 240  # one tip
TRANSPORT_CHARS = 320  # local_transport: "one or two sentences"
PLACE_CHARS = 80  # a place's name


class LocalIntelligence(BaseModel):
    """What a local guide would tell you about the destination — the DestinationIntelligenceAgent's output.

    Saved in an itinerary's `structured_data` under "local_intelligence". It
    comes from a model's own knowledge, not from a search: it is shown as
    advice to check locally, and nothing in a plan is chosen or priced by it.
    """

    local_transport: str | None = None
    cultural_norms: list[str] = Field(default_factory=list)
    tourist_traps: list[str] = Field(default_factory=list)
    best_times: dict[str, str] = Field(default_factory=dict)  # place → when to go, and why
    safety_tips: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.local_transport or self.cultural_norms or self.tourist_traps or self.best_times or self.safety_tips
        )
