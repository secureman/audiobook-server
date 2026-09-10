import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

import database as db
from models import LoginRequest, RegisterRequest, TokenResponse, UserPublic
from security import (
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)

logger = logging.getLogger("router.auth")

router = APIRouter()

# Username: 3-32 chars, ASCII letters / digits / underscore / hyphen.
# Lower-cased before storage; uniqueness is enforced COLLATE NOCASE.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Username must be 3-32 characters of letters, digits, "
                "'_' or '-'"
            ),
        )


def _to_public(user: dict) -> UserPublic:
    return UserPublic(
        id=user["id"],
        username=user["username"],
        created_at=user.get("created_at"),
        last_login_at=user.get("last_login_at"),
    )


@router.post("/auth/register", response_model=TokenResponse)
async def register(req: RegisterRequest) -> TokenResponse:
    _validate_username(req.username)

    existing = await db.get_user_by_username(req.username)
    if existing is not None:
        # Don't leak whether the username is taken vs. wrong password on
        # later login — but for register, "already exists" is the expected
        # UX signal so we surface it directly.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )

    user_id = uuid.uuid4().hex
    password_hash = hash_password(req.password)
    await db.insert_user(user_id, req.username, password_hash)

    # Stamp + reload so the response carries the fresh last_login_at
    # (avoids the client seeing a null then having to call /me to refresh).
    await db.stamp_last_login(user_id)
    user = await db.get_user_by_id(user_id)
    assert user is not None  # we just inserted it

    token = create_token(user_id)
    logger.info("Registered user id=%s username=%s", user_id, req.username)
    return TokenResponse(token=token, user=_to_public(user))


@router.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest) -> TokenResponse:
    user = await db.get_user_by_username(req.username)
    # Same response shape whether the username doesn't exist or the
    # password is wrong, so an attacker can't enumerate usernames.
    if user is None or not verify_password(req.password, user["password_hash"]):
        # Constant-ish time: still hashes even when user is None (verify
        # against a known-bad hash) so timing alone can't reveal which.
        if user is None:
            verify_password(req.password,
                            "pbkdf2_sha256$200000$AAAA$BBBB")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    await db.stamp_last_login(user["id"])
    user = await db.get_user_by_id(user["id"])  # reload to get new stamp
    assert user is not None

    token = create_token(user["id"])
    logger.info("Login: id=%s username=%s", user["id"], user["username"])
    return TokenResponse(token=token, user=_to_public(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(_user: dict = Depends(get_current_user)) -> None:
    # Stateless JWT — the server has nothing to invalidate. Client just
    # discards the token. Kept as an endpoint so the client has an
    # unambiguous "I'm logged out now" call to make, and we can add
    # server-side revocation later if needed (see README).
    logger.info("Logout: id=%s", _user["id"])
    return None


@router.get("/auth/me", response_model=UserPublic)
async def me(user: dict = Depends(get_current_user)) -> UserPublic:
    return _to_public(user)