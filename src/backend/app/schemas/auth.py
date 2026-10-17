import uuid

from pydantic import BaseModel, EmailStr, Field

from app.schemas.text import EMAIL_CHARS, NewPassword
from app.schemas.types import UTCDateTime


class UserCreate(BaseModel):
    email: EmailStr = Field(max_length=EMAIL_CHARS)
    # Phase 35: 8 characters at least (it was only the page that said so), and no more than bcrypt reads
    password: NewPassword


class UserRead(BaseModel):
    id: uuid.UUID
    email: str
    created_at: UTCDateTime

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # Phase 35: how long the access token is good for, in seconds, and the token to get the next one with
    expires_in: int | None = None
    refresh_token: str | None = None
