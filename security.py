"""Password hashing + JWT issue/decode + auth dependency.

We deliberately avoid `bcrypt` / `argon2-cffi` because both have C or Rust
extensions that lack prebuilt Termux / Android wheels. PBKDF2-HMAC-SHA256
from Python's stdlib is pure Python, widely vetted, and 200k iterations is
the OWASP-recommended baseline for 2024+ hardware — takes ~50-100ms per
hash on a phone, fine for a register endpoint that runs once per account.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status

import database as db
from config import settings

logger = logging.getLogger("security")

# ── Password hashing ───────────────────────────────────────────────────

# Format on disk: "pbkdf2_sha256$<iterations>$<base64-salt>$<base64-hash>"
_PBKDF2_PREFIX = "pbkdf2_sha256"
_SALT_BYTES = 16
_HASH_BYTES = 32


def hash_password(plain: str) -> str:
    salt = secrets.token_urlsafe(_SALT_BYTES).encode("ascii")
    digest = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt,
        settings.PBKDF2_ITERATIONS, dklen=_HASH_BYTES,
    )
    return (
        f"{_PBKDF2_PREFIX}${settings.PBKDF2_ITERATIONS}"
        f"${base64.urlsafe_b64encode(salt).decode('ascii')}"
        f"${base64.urlsafe_b64encode(digest).decode('ascii')}"
    )


def verify_password(plain: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
    except ValueError:
        return False
    if algo != _PBKDF2_PREFIX:
        return False
    try:
        iters = int(iters_s)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(hash_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, iters, dklen=len(expected),
    )
    return hmac.compare_digest(actual, expected)


# ── JWT ────────────────────────────────────────────────────────────────


def create_token(user_id: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + settings.JWT_EXPIRY_DAYS * 24 * 3600,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> Optional[str]:
    """Returns user_id on success, None on any failure (expired,
    malformed, bad signature). Never raises — callers decide how to react.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


# ── Auth dependency ────────────────────────────────────────────────────


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """FastAPI dependency. Extracts the bearer token, decodes it, fetches
    the user. Raises 401 with a consistent shape on any failure.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = decode_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await db.get_user_by_id(user_id)
    if user is None:
        # Token was valid but user has been deleted since.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user