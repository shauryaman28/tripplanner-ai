"""
Auth routes.

POST /auth/register   { email, password }  → UserRead (201)
POST /auth/login      form(username, password) → Token (200)

/auth/login accepts the OAuth2PasswordRequestForm so it works out of the
box with FastAPI's Swagger UI 'Authorize' button.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import Token, UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=201)
async def register(body: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    """Create a new user account. Returns 400 if email is already registered."""
    existing = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered.",
        )

    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return a JWT. form.username should be the user's email address."""
    user = (
        await db.execute(select(User).where(User.email == form.username))
    ).scalar_one_or_none()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {"access_token": create_access_token(str(user.id)), "token_type": "bearer"}
