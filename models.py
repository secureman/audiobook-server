"""Pydantic v1 request/response models.

Pinned to v1 to match transcription_server (pydantic v2's compiled core
needs Rust toolchain to build, which Termux / many Linux distros don't have
prebuilt wheels for). Same FastAPI version constraints apply.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


# ── Auth ───────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserPublic(BaseModel):
    id: str
    username: str
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None


class TokenResponse(BaseModel):
    token: str
    user: UserPublic


# ── Progress ───────────────────────────────────────────────────────────


class BookProgress(BaseModel):
    abs_item_id: str
    is_finished: bool
    last_chapter_index: Optional[int] = None
    last_position_seconds: Optional[float] = None
    # Whole-book 0..1 fraction, rolled by the client player. 1.0 when
    # is_finished is true. Lets the library list show a progress strip
    # without re-deriving it from chapter bookmarks.
    progress: Optional[float] = None
    updated_at: Optional[str] = None


class SetFinishedRequest(BaseModel):
    is_finished: bool


class SetBookProgressRequest(BaseModel):
    """Full book-level progress upsert — the direct replacement for the old
    ABS `/api/me/progress` PATCH body. Sent by the player on its rolling
    sync so the bookmark AND the whole-book fraction stay fresh.
    """
    last_chapter_index: int = Field(ge=0)
    last_position_seconds: float = Field(ge=0.0)
    progress: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class SetBookChaptersRequest(BaseModel):
    """True replace-set semantics for the whole-book listened toggle: after
    this call the book's chapter_done set for this user is exactly
    [chapters] (empty list ⇒ all cleared).
    """
    chapters: List[int] = Field(default_factory=list)


class SetPositionRequest(BaseModel):
    """Used both for the per-chapter position and (by extension) the
    'last played bookmark' on a book — same shape, different endpoint.
    """
    position_seconds: float = Field(ge=0.0)


class BulkProgress(BaseModel):
    """The response shape for GET /api/progress — designed to populate
    the Flutter client's three local providers (ReadChaptersController,
    ChapterPositionsController, BookPlaybackProgress) in one round trip.
    """
    books: dict[str, BookProgress] = Field(default_factory=dict)
    # abs_item_id -> list of chapter_index
    chapters_done: dict[str, list[int]] = Field(default_factory=dict)
    # abs_item_id -> {chapter_index: position_seconds}
    chapter_positions: dict[str, dict[int, float]] = Field(
        default_factory=dict
    )