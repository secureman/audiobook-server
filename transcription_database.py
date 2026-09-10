import asyncio
import json
import logging
from datetime import datetime
from contextlib import asynccontextmanager

import aiosqlite

from config import settings

logger = logging.getLogger("database")

DB_PATH = settings.TRANSCRIPTION_DB_PATH

# Module-level lock serializes all writes (aiosqlite is single-connection;
# this makes claim/upsert/status atomic).
_lock = asyncio.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    title TEXT,
    author TEXT,
    total_chapters INTEGER,
    abs_item_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transcription_jobs (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    chapter_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    vtt_path TEXT,
    error_message TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(book_id, chapter_index)
);
"""


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return {r[1] for r in rows}


async def init_db() -> None:
    """Create tables if missing, then apply additive migrations for older DBs.

    Older databases may be missing: progress, priority, started_at, finished_at.
    Each ALTER is idempotent (skipped if the column already exists).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        cols = await _table_columns(db, "transcription_jobs")
        migrations = [
            ("progress", "ALTER TABLE transcription_jobs "
                        "ADD COLUMN progress REAL NOT NULL DEFAULT 0"),
            ("priority", "ALTER TABLE transcription_jobs "
                        "ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"),
            ("started_at", "ALTER TABLE transcription_jobs "
                          "ADD COLUMN started_at TIMESTAMP"),
            ("finished_at", "ALTER TABLE transcription_jobs "
                           "ADD COLUMN finished_at TIMESTAMP"),
        ]
        for col, sql in migrations:
            if col not in cols:
                logger.info("Migrating: adding %s column", col)
                await db.execute(sql)
        await db.commit()


@asynccontextmanager
async def _connect():
    db = await aiosqlite.connect(DB_PATH)
    try:
        yield db
        await db.commit()
    finally:
        await db.close()


async def reset_stuck_processing() -> int:
    """Rows left in 'processing' from a previous crash are failed over to
    'pending' so the worker pool picks them up again on next boot.

    Returns the number of rows reset (for logging).
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE transcription_jobs SET status = 'pending', "
                "updated_at = CURRENT_TIMESTAMP WHERE status = 'processing'"
            )
            await db.commit()
            return cur.rowcount or 0


# ── Books ──────────────────────────────────────────────────────────────


async def upsert_book(item_id: str, title: str, author: str,
                      total_chapters: int, abs_item_json: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO books (id, title, author, total_chapters, abs_item_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                author=excluded.author,
                total_chapters=excluded.total_chapters,
                abs_item_json=excluded.abs_item_json
            """,
            (item_id, title, author, total_chapters,
             json.dumps(abs_item_json)),
        )
        await db.commit()


async def get_book(item_id: str) -> dict | None:
    """Returns the cached ABS item JSON, or None if not cached."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT abs_item_json FROM books WHERE id = ?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return json.loads(row["abs_item_json"])


# ── Jobs ───────────────────────────────────────────────────────────────


async def upsert_job(book_id: str, chapter_index: int,
                    priority: int = 0) -> str | None:
    """Inserts a pending job. Returns None if a done job already exists.

    [priority] 0 = bulk-enqueued (e.g. "transcribe whole book"),
    10 = user-asked-for (e.g. tapping a single chapter). Higher = picked
    first by claim_job.
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            # Skip chapters already done. If a job exists and is still
            # pending/processing/erroring, upgrade its priority so the
            # user's explicit "transcribe this NOW" request jumps the queue.
            async with db.execute(
                "SELECT status, priority FROM transcription_jobs "
                "WHERE book_id = ? AND chapter_index = ?",
                (book_id, chapter_index),
            ) as cur:
                row = await cur.fetchone()
            if row is not None and row[0] == "done":
                return None
            if row is not None and priority > (row[1] or 0):
                await db.execute(
                    "UPDATE transcription_jobs SET priority = ?, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE book_id = ? AND chapter_index = ?",
                    (priority, book_id, chapter_index),
                )

            job_id = f"{book_id}-{chapter_index}"
            await db.execute(
                """
                INSERT INTO transcription_jobs
                    (id, book_id, chapter_index, status, priority)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(book_id, chapter_index) DO UPDATE SET
                    status='pending',
                    priority=MAX(priority, excluded.priority),
                    error_message=NULL,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (job_id, book_id, chapter_index, priority),
            )
            await db.commit()
        return job_id


async def set_job_status(job_id: str, status: str,
                         vtt_path: str | None = None,
                         error_message: str | None = None,
                         progress: float | None = None) -> None:
    """Update job state, automatically stamping started_at / finished_at
    on the appropriate transitions.
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            # Set started_at on the first transition to 'processing' only.
            if status == "processing":
                await db.execute(
                    """
                    UPDATE transcription_jobs
                    SET status = ?,
                        vtt_path = COALESCE(?, vtt_path),
                        error_message = ?,
                        progress = COALESCE(?, progress),
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, vtt_path, error_message, progress, job_id),
                )
            elif status in ("done", "error"):
                await db.execute(
                    """
                    UPDATE transcription_jobs
                    SET status = ?,
                        vtt_path = COALESCE(?, vtt_path),
                        error_message = ?,
                        progress = COALESCE(?, progress),
                        finished_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, vtt_path, error_message, progress, job_id),
                )
            else:
                await db.execute(
                    """
                    UPDATE transcription_jobs
                    SET status = ?,
                        vtt_path = COALESCE(?, vtt_path),
                        error_message = ?,
                        progress = COALESCE(?, progress),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, vtt_path, error_message, progress, job_id),
                )
            await db.commit()


async def set_job_progress(job_id: str, progress: float) -> None:
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE transcription_jobs SET progress = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (progress, job_id),
            )
            await db.commit()


async def claim_job() -> str | None:
    """Atomically pops the highest-priority pending job and marks it
    processing. Within the same priority, FIFO by created_at.

    Returns the job_id or None if no pending jobs.
    """
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id FROM transcription_jobs "
                "WHERE status = 'pending' "
                "ORDER BY priority DESC, created_at ASC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            job_id = row[0]
            await db.execute(
                "UPDATE transcription_jobs SET status = 'processing', "
                "started_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,))
            await db.commit()
    return job_id


async def get_active_jobs() -> list[dict]:
    """All jobs currently in 'processing' state, with book & chapter
    metadata joined in for the /api/jobs/active endpoint.

    Joins on the books table (which caches the full ABS item JSON) and
    extracts the chapter title from there.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT j.id          AS job_id,
                   j.book_id     AS book_id,
                   j.chapter_index,
                   j.progress,
                   j.started_at,
                   b.title       AS book_title,
                   b.abs_item_json
              FROM transcription_jobs j
              LEFT JOIN books b ON b.id = j.book_id
             WHERE j.status = 'processing'
             ORDER BY j.started_at ASC
            """
        ) as cur:
            rows = await cur.fetchall()
    out: list[dict] = []
    now = datetime.utcnow()
    for r in rows:
        chapter_title = f"Chapter {r['chapter_index'] + 1}"
        duration_seconds = 0
        try:
            if r["abs_item_json"]:
                item = json.loads(r["abs_item_json"])
                chapters = (item.get("media") or {}).get("chapters") or []
                if r["chapter_index"] < len(chapters):
                    chapter_title = (chapters[r["chapter_index"]]
                                     .get("title") or chapter_title)
        except (ValueError, TypeError):
            pass
        if r["started_at"]:
            try:
                started = datetime.fromisoformat(
                    r["started_at"].replace("Z", ""))
                duration_seconds = max(0, int((now - started).total_seconds()))
            except (ValueError, TypeError):
                pass
        out.append({
            "job_id": r["job_id"],
            "book_id": r["book_id"],
            "book_title": r["book_title"] or "Unknown",
            "chapter_index": r["chapter_index"],
            "chapter_title": chapter_title,
            "progress": float(r["progress"] or 0),
            "started_at": r["started_at"],
            "duration_seconds": duration_seconds,
        })
    return out


async def get_job(job_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_jobs_for_book(book_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transcription_jobs "
            "WHERE book_id = ? ORDER BY chapter_index",
            (book_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_incomplete_jobs(book_id: str) -> int:
    """Number of queued/running jobs for a book (pending or processing).

    Used to decide when the book's audio cache can be deleted: once this
    returns 0, every chapter has finished and the cache is safe to remove.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transcription_jobs "
            "WHERE book_id = ? AND status IN ('pending', 'processing')",
            (book_id,),
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def job_status_counts() -> dict[str, int]:
    """Counts of transcription_jobs rows grouped by status, for a one-line
    startup sanity check — see main.py's lifespan handler.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, COUNT(*) FROM transcription_jobs "
            "GROUP BY status"
        ) as cur:
            rows = await cur.fetchall()
    return {status: count for status, count in rows}
