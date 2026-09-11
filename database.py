import asyncio
import logging
import uuid

import aiosqlite

from config import settings

logger = logging.getLogger("database")

DB_PATH = settings.METADATA_DB_PATH

# Module-level lock serializes all writes (aiosqlite is single-connection;
# this makes upsert / mark-done / unmark atomic).
_lock = asyncio.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS book_progress (
    user_id               TEXT NOT NULL,
    abs_item_id           TEXT NOT NULL,
    is_finished           INTEGER NOT NULL DEFAULT 0,
    last_chapter_index    INTEGER,
    last_position_seconds REAL,
    progress              REAL,
    updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, abs_item_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chapter_done (
    user_id       TEXT NOT NULL,
    abs_item_id   TEXT NOT NULL,
    chapter_index INTEGER NOT NULL,
    marked_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, abs_item_id, chapter_index),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chapter_position (
    user_id          TEXT NOT NULL,
    abs_item_id      TEXT NOT NULL,
    chapter_index    INTEGER NOT NULL,
    position_seconds REAL NOT NULL,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, abs_item_id, chapter_index),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chapter_done_user_book
    ON chapter_done(user_id, abs_item_id);
CREATE INDEX IF NOT EXISTS idx_chapter_position_user_book
    ON chapter_position(user_id, abs_item_id);
"""


async def init_db() -> None:
    """Create tables / indexes if missing and enable foreign keys.

    SQLite defaults FK enforcement to OFF; we need it ON for the
    ON DELETE CASCADE on user_id to fire when a user is deleted.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(_SCHEMA)
        await db.commit()
        # Idempotent migrations for DBs created before a column existed
        # (older metadata.db files lack `book_progress.progress`).
        try:
            await db.execute(
                "ALTER TABLE book_progress ADD COLUMN progress REAL"
            )
            await db.commit()
        except aiosqlite.OperationalError:
            # Column already present — nothing to do.
            await db.rollback()


# ── Users ──────────────────────────────────────────────────────────────


async def get_user_by_username(username: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def insert_user(user_id: str, username: str,
                      password_hash: str) -> None:
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (?, ?, ?)",
                (user_id, username, password_hash),
            )
            await db.commit()


async def stamp_last_login(user_id: str) -> None:
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET last_login_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (user_id,),
            )
async def stamp_last_login(user_id: str) -> None:
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET last_login_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (user_id,),
            )
            await db.commit()


async def get_or_create_api_user() -> dict:
    """The singleton row that owns everything written via API-key auth.

    The Flutter client no longer logs in — it sends its Audiobookshelf
    token as an X-API-Key header on every metadata/progress call. All
    that data needs one user row to attach to (progress tables key off
    users.id), so every validated API key maps to the same
    username='api_key' account, created on first use with an unusable
    random password hash (nobody can log in to it, but it satisfies the
    NOT NULL column).
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM users WHERE username = 'api_key' COLLATE NOCASE"
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                return dict(row)
            user_id = uuid.uuid4().hex
            # Unusable hash: not a valid PBKDF2 record, so login against
            # this account always fails even if someone guesses the name.
            await conn.execute(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (?, 'api_key', 'api_key-no-login')",
                (user_id,),
            )
            await conn.commit()
            async with conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
    return dict(row) if row else {}


async def delete_user(user_id: str) -> None:
    """Cascades to all progress tables via ON DELETE CASCADE."""
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await db.commit()


# ── Book progress ───────────────────────────────────────────────────────


async def upsert_book_finished(user_id: str, abs_item_id: str,
                               is_finished: bool) -> dict:
    """Set / clear the finished flag for a (user, book) row. Idempotent —
    always returns the resulting row so callers can echo it back.
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO book_progress
                    (user_id, abs_item_id, is_finished)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, abs_item_id) DO UPDATE SET
                    is_finished = excluded.is_finished,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, abs_item_id, 1 if is_finished else 0),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM book_progress "
                "WHERE user_id = ? AND abs_item_id = ?",
                (user_id, abs_item_id),
            ) as cur:
                row = await cur.fetchone()
    return dict(row) if row else {}


async def upsert_book_position(user_id: str, abs_item_id: str,
                                chapter_index: int,
                                position_seconds: float) -> dict:
    """Update last_chapter_index + last_position_seconds on the book row
    (the 'continue reading' bookmark). Creates the row if missing.
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO book_progress
                    (user_id, abs_item_id, last_chapter_index,
                     last_position_seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, abs_item_id) DO UPDATE SET
                    last_chapter_index    = excluded.last_chapter_index,
                    last_position_seconds = excluded.last_position_seconds,
                    updated_at            = CURRENT_TIMESTAMP
                """,
                (user_id, abs_item_id, chapter_index, position_seconds),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM book_progress "
                "WHERE user_id = ? AND abs_item_id = ?",
                (user_id, abs_item_id),
            ) as cur:
                row = await cur.fetchone()
    return dict(row) if row else {}


async def upsert_book_progress(
    user_id: str,
    abs_item_id: str,
    last_chapter_index: int,
    last_position_seconds: float,
    progress: float | None,
) -> dict:
    """Full book-level upsert — bookmark + whole-book fraction in one write.
    This is the direct replacement for the old ABS `/api/me/progress` PATCH
    the Flutter player used to make. Returns the resulting row.
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO book_progress
                    (user_id, abs_item_id, last_chapter_index,
                     last_position_seconds, progress)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, abs_item_id) DO UPDATE SET
                    last_chapter_index    = excluded.last_chapter_index,
                    last_position_seconds = excluded.last_position_seconds,
                    progress              = excluded.progress,
                    updated_at            = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    abs_item_id,
                    last_chapter_index,
                    last_position_seconds,
                    progress,
                ),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM book_progress "
                "WHERE user_id = ? AND abs_item_id = ?",
                (user_id, abs_item_id),
            ) as cur:
                row = await cur.fetchone()
    return dict(row) if row else {}


async def get_book_progress(user_id: str, abs_item_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM book_progress "
            "WHERE user_id = ? AND abs_item_id = ?",
            (user_id, abs_item_id),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_book_progress(user_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM book_progress WHERE user_id = ?",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Chapter done / position ─────────────────────────────────────────────


async def mark_chapter_done(user_id: str, abs_item_id: str,
                            chapter_index: int) -> None:
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO chapter_done "
                "(user_id, abs_item_id, chapter_index) VALUES (?, ?, ?)",
                (user_id, abs_item_id, chapter_index),
            )
            await db.commit()


async def unmark_chapter_done(user_id: str, abs_item_id: str,
                              chapter_index: int) -> None:
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM chapter_done "
                "WHERE user_id = ? AND abs_item_id = ? AND chapter_index = ?",
                (user_id, abs_item_id, chapter_index),
            )
            await db.commit()


async def replace_book_chapters(user_id: str, abs_item_id: str,
                                chapters: list[int]) -> None:
    """Replaces the book's whole chapter_done set with [chapters]
    (empty ⇒ all cleared). One round trip for the whole-book listened
    toggle instead of N per-chapter calls.
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM chapter_done "
                "WHERE user_id = ? AND abs_item_id = ?",
                (user_id, abs_item_id),
            )
            if chapters:
                await db.executemany(
                    "INSERT OR IGNORE INTO chapter_done "
                    "(user_id, abs_item_id, chapter_index) VALUES (?, ?, ?)",
                    [(user_id, abs_item_id, c) for c in chapters],
                )
            await db.commit()


async def get_chapters_done(user_id: str, abs_item_id: str) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chapter_index FROM chapter_done "
            "WHERE user_id = ? AND abs_item_id = ? "
            "ORDER BY chapter_index",
            (user_id, abs_item_id),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_all_chapters_done(user_id: str) -> list[tuple[str, int]]:
    """All (abs_item_id, chapter_index) pairs marked done for this user.
    Used by the bulk /progress endpoint.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT abs_item_id, chapter_index FROM chapter_done "
            "WHERE user_id = ? ORDER BY abs_item_id, chapter_index",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [(r[0], r[1]) for r in rows]


async def upsert_chapter_position(user_id: str, abs_item_id: str,
                                  chapter_index: int,
                                  position_seconds: float) -> None:
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO chapter_position
                    (user_id, abs_item_id, chapter_index, position_seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, abs_item_id, chapter_index) DO UPDATE SET
                    position_seconds = excluded.position_seconds,
                    updated_at       = CURRENT_TIMESTAMP
                """,
                (user_id, abs_item_id, chapter_index, position_seconds),
            )
            await db.commit()


async def get_chapter_positions(
    user_id: str, abs_item_id: str
) -> dict[int, float]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chapter_index, position_seconds FROM chapter_position "
            "WHERE user_id = ? AND abs_item_id = ?",
            (user_id, abs_item_id),
        ) as cur:
            rows = await cur.fetchall()
    return {r[0]: float(r[1]) for r in rows}


async def get_all_chapter_positions(user_id: str) -> list[tuple[str, int, float]]:
    """All (abs_item_id, chapter_index, position_seconds) for this user.
    Used by the bulk /progress endpoint.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT abs_item_id, chapter_index, position_seconds "
            "FROM chapter_position WHERE user_id = ? "
            "ORDER BY abs_item_id, chapter_index",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [(r[0], r[1], float(r[2])) for r in rows]