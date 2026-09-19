from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.core.preferences import MAX_ITEM_LENGTH, MAX_LIST_ITEMS, normalise_airlines, normalise_dietary
from app.models.user_preferences import TravelStyle

DietaryItem = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ITEM_LENGTH)]
# Case-insensitive on input; canonicalised to upper-case by the validator below.
AirlineCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9]{2}$")]


class PreferencesUpdate(BaseModel):
    """Body for PUT /users/preferences — a FULL overwrite.

    Any field omitted from the body is reset to its default (empty list /
    null). This is what distinguishes the endpoint from the additive
    PreferenceExtractor, which only ever adds information.

    preferred_airlines are 2-character IATA carrier codes (e.g. "6E" for
    IndiGo, "AI" for Air India) because they are matched against the
    carrier code Amadeus returns.
    """

    dietary_restrictions: list[DietaryItem] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    preferred_airlines: list[AirlineCode] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    travel_style: TravelStyle | None = None
    home_city: str | None = Field(default=None, max_length=100)

    @field_validator("dietary_restrictions")
    @classmethod
    def _canonical_dietary(cls, v: list[str]) -> list[str]:
        return normalise_dietary(v)

    @field_validator("preferred_airlines")
    @classmethod
    def _canonical_airlines(cls, v: list[str]) -> list[str]:
        return normalise_airlines(v)

    @field_validator("home_city")
    @classmethod
    def _blank_city_is_none(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v else None


class PreferencesRead(BaseModel):
    """Response for GET/PUT /users/preferences.

    Defaults make ``PreferencesRead()`` the "nothing saved yet" response, so
    GET returns 200 with empty preferences rather than a 404 for new users.
    """

    dietary_restrictions: list[str] = Field(default_factory=list)
    preferred_airlines: list[str] = Field(default_factory=list)
    travel_style: str | None = None
    home_city: str | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
