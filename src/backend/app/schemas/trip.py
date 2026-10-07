import uuid
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.schemas.types import UTCDateTime
from src.ai import group

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# The searches return at most ten attractions and the builder writes every day in one reply:
# past two weeks a plan is mostly empty days, and a year-long one came back as a single day.
MAX_TRIP_NIGHTS = 14


class GroupMember(BaseModel):
    """One traveller of a group trip (Phase 25): a name, and what they enjoy.

    Both are a few plain words — they are shown to the model that writes the
    plan, so nothing that could pass for an instruction or break out of a line
    is let in. No interests means "happy with whatever the others choose".
    """

    name: NonBlank
    interests: list[NonBlank] = Field(default_factory=list, max_length=group.MAX_INTERESTS)

    @field_validator("name")
    @classmethod
    def name_is_a_name(cls, v: str) -> str:
        if not group.is_words(v, group.MAX_NAME_CHARS):
            raise ValueError(f"a name is at most {group.MAX_NAME_CHARS} letters, digits, spaces or simple punctuation")
        return v

    @field_validator("interests")
    @classmethod
    def interests_are_words(cls, v: list[str]) -> list[str]:
        for interest in v:
            if not group.is_words(interest, group.MAX_INTEREST_CHARS):
                raise ValueError(
                    f"an interest is at most {group.MAX_INTEREST_CHARS} letters, digits, spaces or simple punctuation"
                )
        return list(dict.fromkeys(v))  # each once, in the order given


class TripCreate(BaseModel):
    destination: Annotated[NonBlank, Field(max_length=200)]
    start_date: date
    end_date: date
    budget: float = Field(gt=0)
    group_size: int = Field(default=1, ge=1, le=9)  # 9 = the flight search's passenger limit
    interests: list[NonBlank] | None = None
    # Phase 25: the travellers, told apart. Left out, the trip is planned as before — for
    # `group_size` people who all enjoy `interests`.
    group_members: list[GroupMember] | None = Field(
        default=None, min_length=group.MIN_MEMBERS, max_length=group.MAX_MEMBERS
    )

    @model_validator(mode="after")
    def the_group_adds_up(self) -> "TripCreate":
        """Named travellers are counted, and what they enjoy is what the trip is about."""
        if not self.group_members:
            return self
        names = [member.name.casefold() for member in self.group_members]
        if len(set(names)) != len(names):
            raise ValueError("group_members: two travellers have the same name")
        if "group_size" not in self.model_fields_set:
            self.group_size = len(self.group_members)  # nobody said how many: as many as were named
        elif self.group_size < len(self.group_members):
            raise ValueError(f"group_size is {self.group_size}, but {len(self.group_members)} travellers are named")
        everyones = [interest for member in self.group_members for interest in member.interests]
        self.interests = list(dict.fromkeys([*(self.interests or []), *everyones])) or None
        return self

    @field_validator("start_date")
    @classmethod
    def start_not_in_past(cls, v: date) -> date:
        if v < date.today():
            raise ValueError("start_date cannot be in the past")
        return v

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        if "start_date" in info.data and v <= info.data["start_date"]:
            raise ValueError("end_date must be after start_date")
        if "start_date" in info.data and (v - info.data["start_date"]).days > MAX_TRIP_NIGHTS:
            raise ValueError(f"a trip can be at most {MAX_TRIP_NIGHTS} nights long")
        return v


class TripRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    destination: str
    start_date: date
    end_date: date
    budget: float
    group_size: int
    interests: list[str] | None
    group_members: list[GroupMember] | None = None  # Phase 25
    status: str
    created_at: UTCDateTime

    model_config = {"from_attributes": True}


class TripMatch(BaseModel):
    """A trip found by similarity (Phase 23): enough to show it as a card, and how close it came."""

    trip: TripRead
    itinerary_id: uuid.UUID  # the itinerary it was matched on — the trip's latest
    total_cost: float | None
    highlight: str | None  # one place to name the trip by: its most popular stop
    similarity: float  # cosine similarity of the two embeddings; 1 is the same


class SimilarTripsResponse(BaseModel):
    """GET /trips/{id}/similar.

    status "pending": this trip's own embedding is not there yet — it is made
    in the background just after a plan is saved — so nothing can be compared.
    """

    status: Literal["ready", "pending"]
    results: list[TripMatch]


class TripSearchResponse(BaseModel):
    """GET /trips/search?q=…"""

    query: str
    results: list[TripMatch]


class PlanRequest(BaseModel):
    """Optional body for POST /trips/{id}/plan."""

    raw_input: str | None = None


class ClarifyRequest(BaseModel):
    """Body for POST /trips/{id}/clarify."""

    answer: NonBlank


class RefineRequest(BaseModel):
    """Body for POST /trips/{id}/refine — Phase 15."""

    message: NonBlank


class ReplanRequest(BaseModel):
    """Body for POST /trips/{id}/replan — Phase 10.

    choice meanings:
      cheaper_flights  → re-run flight search with a reduced budget cap and one more stop
      reduce_days      → shorten the trip to the length the conflict offered (Phase 21; 2 days fewer before)
      increase_budget  → raise the budget to what the flights need (at least +25%) and retry
      cheaper_hotel    → go ahead with a cheaper stay (Phase 21)
      off_peak         → move the trip to the destination's off-season (Phase 21)
    """

    choice: Literal["cheaper_flights", "reduce_days", "increase_budget", "cheaper_hotel", "off_peak"]


class RetryRequest(BaseModel):
    """Optional body for POST /trips/{id}/retry — Phase 20.

    agent: the search to run again on a trip that has a plan (the plan keeps everything else).
    Left out — or when the trip has no plan yet — the whole trip is planned again.
    """

    agent: Literal["flight_agent", "hotel_agent", "activities_agent"] | None = None
