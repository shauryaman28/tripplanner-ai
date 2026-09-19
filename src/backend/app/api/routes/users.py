"""
User routes — all require JWT.

GET /users/preferences    current preferences (empty defaults if none saved yet)
PUT /users/preferences    full overwrite of the user's preferences

PUT is a REPLACE: fields omitted from the body are reset to their defaults.
The PreferenceExtractor (src/ai/agents/preference_extractor.py) is the
additive counterpart — it only ever adds to what is stored here.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.models.user_preferences import UserPreferences
from app.schemas.user_preferences import PreferencesRead, PreferencesUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/preferences", response_model=PreferencesRead)
async def get_preferences(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PreferencesRead:
    """Return the caller's saved preferences, or empty defaults for a new user."""
    prefs = await db.get(UserPreferences, current_user.id)
    return PreferencesRead.model_validate(prefs) if prefs is not None else PreferencesRead()


@router.put("/preferences", response_model=PreferencesRead)
async def put_preferences(
    body: PreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserPreferences:
    """Create or fully overwrite the caller's preferences."""
    prefs = await db.get(UserPreferences, current_user.id)
    if prefs is None:
        prefs = UserPreferences(user_id=current_user.id)

    # Assign fresh lists (never mutate in place) so SQLAlchemy sees the change on JSONB columns.
    prefs.dietary_restrictions = body.dietary_restrictions
    prefs.preferred_airlines = body.preferred_airlines
    prefs.travel_style = body.travel_style
    prefs.home_city = body.home_city

    db.add(prefs)
    await db.commit()
    await db.refresh(prefs)
    return prefs
