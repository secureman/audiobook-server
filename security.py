"""Password hashing + JWT issue/decode + auth dependency.

We deliberately avoid `bcrypt` / `argon2-cffi` because both have C or Rust
extensions that lack prebuilt Termux / Android wheels. PBKDF2-HMAC-SHA256
from Python's stdlib is pure Python, widely vetted, and 200k iterations is
the OWASP-recommended baseline for 2024+ hardware — takes ~50-100ms per
hash on a phone, fine for a register endpoint that runs once per account.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Optional

import httpx
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


# ── API-key auth (the client's only path) ───────────────────────────────
#
# The Flutter client no longer creates accounts on this server. It sends
# its Audiobookshelf token as `X-API-Key: <ABS token>` and this server
# validates it by asking ABS "who is this?" (GET /api/me). Validation is
# cached in-process for 1h so a steady client doesn't turn every progress
# write into an ABS round trip. All validated keys share one
# get_or_create_api_user() row — progress data has to attach to some
# user, and per-ABS-account data is a non-goal for this self-hosted,
# single-household setup.
#
# The JWT dependency above is untouched and keeps serving /api/auth/*.

_API_CACHE_TTL_S = 3600  # 1 hour
_api_key_cache: dict[str, float] = {}  # key -> expiry (monotonic seconds)
_api_key_cache_lock = asyncio.Lock()


def _api_cache_key(base_url: str, token: str) -> str:
    """Cache key that never mixes ABS instances and never stores raw keys.

    The URL part scopes the cache to one ABS deployment (so pointing the
    client at a different server can't inherit a stale hit); the sha256
    keeps full tokens out of memory dumps and bounds the entry size.
    """
    return hashlib.sha256(f"{base_url}|{token}".encode("utf-8")).hexdigest()


async def get_api_key_user(
    x_api_key: Optional[str] = Header(default=None),
) -> dict:
    """FastAPI dependency for every endpoint the Flutter client calls.

    Validates the Audiobookshelf token against ABS (1h in-memory cache)
    and returns the shared API-key user row. Raises 401 on any failure —
    same error shape as the JWT path.
    """
    if not x_api_key or not x_api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    api_key = x_api_key.strip()

    # 1. Cached validation — skip the ABS round trip entirely.
    now = time.monotonic()
    ck = _api_cache_key(settings.ABS_BASE_URL, api_key)
    async with _api_key_cache_lock:
        cached_expiry = _api_key_cache.get(ck)
        if cached_expiry is not None and cached_expiry > now:
            # Opportunistically drop other expired entries.
            for k in [k for k, exp in _api_key_cache.items() if exp <= now]:
                del _api_key_cache[k]
            return await db.get_or_create_api_user()

    # 2. Ask ABS who owns this token.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{settings.ABS_BASE_URL.rstrip('/')}/api/me",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError:
        logger.warning("API-key check: ABS unreachable (%s)", settings.ABS_BASE_URL)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cannot validate API key (ABS unreachable)",
        )
    if res.status_code != 200:
        logger.info("API-key check: ABS rejected the key (HTTP %s)", res.status_code)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    async with _api_key_cache_lock:
        _api_key_cache[ck] = now + _API_CACHE_TTL_S

    return await db.get_or_create_api_user()