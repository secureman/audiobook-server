"""Unified Audiobook Server — configuration.

One server, two previously-separate halves:
  * metadata / accounts / reading progress   (the old metadata-server)
  * transcription / VTT / Groq Whisper       (the old transcription_server)

Deliberately plain-Python (no pydantic-settings): pydantic v2's compiled
core (pydantic-core) has no prebuilt Android/Termux wheels and needs Rust
to build, which we avoid so the server installs with `pip install` alone
on Termux. Values come from the environment, falling back to a `.env`
file in this repo directory (and the CWD) via python-dotenv.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from paths import resolve_dir, resolve_file

# Load .env from this repo directory first (robust to any CWD), then a
# .env in the CWD if present — same effective precedence users expect from
# pydantic-settings (real environment always wins over .env values).
_REPO_ENV = Path(__file__).resolve().parent / ".env"
load_dotenv(_REPO_ENV, override=False)
load_dotenv(override=False)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _script_dir() -> str:
    """Directory this file lives in — used for repo-local defaults."""
    return str(Path(__file__).resolve().parent)


class Settings:
    # ── Network (shared — one process) ────────────────────────────────
    HOST: str = _env_str("HOST", "0.0.0.0")
    PORT: int = _env_int("PORT", 8001)

    # ── Logging (shared) ──────────────────────────────────────────────
    LOG_DIR: str = _env_str("LOG_DIR", resolve_dir("LOG_DIR", "logs"))

    # ── ABS (shared by both halves) ───────────────────────────────────
    ABS_BASE_URL: str = _env_str("ABS_BASE_URL", "http://localhost:13378")
    ABS_API_TOKEN: str = _env_str("ABS_API_TOKEN", "")

    # ── Metadata half: accounts + reading progress ────────────────────
    # Auth — JWT_SECRET is REQUIRED. assert_jwt_secret_configured() fails
    # loud at startup if it's missing or still the placeholder, because
    # tokens signed with a guessable secret equal no auth at all.
    JWT_SECRET: str = _env_str("JWT_SECRET", "")
    JWT_EXPIRY_DAYS: int = _env_int("JWT_EXPIRY_DAYS", 30)
    # Password hashing (PBKDF2-HMAC-SHA256)
    PBKDF2_ITERATIONS: int = _env_int("PBKDF2_ITERATIONS", 200_000)
    METADATA_DB_PATH: str = _env_str(
        "METADATA_DB_PATH", os.path.join(_script_dir(), "metadata.db")
    )

    # ── Transcription half: jobs / VTT / Groq ─────────────────────────
    GROQ_API_KEY: str = _env_str("GROQ_API_KEY", "")
    OUTPUT_DIR: str = _env_str("OUTPUT_DIR", resolve_dir("OUTPUT_DIR", "vtt_cache"))
    TEMP_DIR: str = _env_str("TEMP_DIR", resolve_dir("TEMP_DIR", "tmp_audio"))
    TRANSCRIPTION_DB_PATH: str = _env_str(
        "TRANSCRIPTION_DB_PATH",
        resolve_file("TRANSCRIPTION_DB_PATH", "transcriptions.db"),
    )
    MAX_CONCURRENT_JOBS: int = _env_int("MAX_CONCURRENT_JOBS", 2)
    # Parallel Groq API calls across all workers. Groq is I/O-bound, so this
    # is deliberately separate from MAX_CONCURRENT_JOBS (which bounds the
    # CPU-bound ffmpeg work). Raise on paid Groq tiers with higher RPM.
    MAX_CONCURRENT_GROQ: int = _env_int("MAX_CONCURRENT_GROQ", 4)
    # Parallel chunk uploads within a single chapter's transcription.
    MAX_CONCURRENT_CHUNKS: int = _env_int("MAX_CONCURRENT_CHUNKS", 4)
    # Target chunk size in MB fed to Groq per request (must stay < 25 MB).
    # Smaller chunks parallelize better; ~10 MB ≈ 20 min of 64 kbps audio.
    GROQ_CHUNK_SIZE_MB: float = _env_float("GROQ_CHUNK_SIZE_MB", 10.0)
    # Whisper model. whisper-large-v3-turbo is ~8x faster with a small
    # accuracy tradeoff; set to whisper-large-v3 for maximum quality.
    GROQ_MODEL: str = _env_str("GROQ_MODEL", "whisper-large-v3-turbo")


settings = Settings()


def assert_jwt_secret_configured() -> None:
    """Call once at startup. Raises if JWT_SECRET is missing or the
    placeholder — refusing to boot is better than silently accepting tokens
    signed with a public default.
    """
    if not settings.JWT_SECRET or settings.JWT_SECRET.startswith("change-me"):
        raise RuntimeError(
            "JWT_SECRET is not configured. Set it in .env "
            "(repo root) to a long random string (e.g. `python -c "
            "\"import secrets; print(secrets.token_urlsafe(48))\"`). "
            "The server refuses to boot otherwise."
        )
