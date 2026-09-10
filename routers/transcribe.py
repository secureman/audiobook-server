import logging

import httpx
from fastapi import APIRouter, HTTPException

import transcription_database as db
from transcription_models import JobStatus, TranscribeRequest
from services import abs_client, queue

logger = logging.getLogger("router.transcribe")

router = APIRouter()

# Priority constants. Higher = picked first by claim_job().
PRIORITY_USER = 10   # explicit "transcribe this chapter NOW" from the user
PRIORITY_BULK = 0    # bulk enqueue (e.g. "transcribe whole book")


async def _ensure_book_cached(item_id: str) -> dict:
    """Fetches and caches book metadata from ABS if not already in DB."""
    cached = await db.get_book(item_id)
    if cached is not None:
        return cached
    try:
        item_json = await abs_client.get_item(item_id)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 401:
            raise HTTPException(
                502, "ABS rejected the token — check ABS_API_TOKEN in .env"
            ) from e
        raise HTTPException(502, f"ABS returned HTTP {code}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Cannot reach ABS: {e}") from e
    meta = abs_client.extract_meta(item_json)
    await db.upsert_book(item_id, meta["title"], meta["author"],
                         len(meta["chapters"]), item_json)
    return item_json


def _resolve_indices(req: TranscribeRequest,
                     total_chapters: int) -> list[int]:
    if req.mode == "full":
        return list(range(total_chapters))

    if req.mode == "chapter":
        if req.chapter_index is None:
            raise HTTPException(
                422, "chapter_index is required when mode='chapter'")
        if not 0 <= req.chapter_index < total_chapters:
            raise HTTPException(422, "chapter_index out of range")
        return [req.chapter_index]

    # mode == "range"
    if req.from_chapter is None or req.count is None:
        raise HTTPException(
            422, "from_chapter and count are required when mode='range'")
    start = max(0, req.from_chapter)
    end = min(total_chapters, start + req.count)
    if start >= end:
        raise HTTPException(422, "empty chapter range")
    return list(range(start, end))


@router.post("/transcribe")
async def transcribe(req: TranscribeRequest) -> dict:
    logger.info(
        "Transcribe request: item=%s mode=%s chapter_index=%s "
        "from_chapter=%s count=%s",
        req.abs_item_id, req.mode, req.chapter_index,
        req.from_chapter, req.count,
    )
    item_json = await _ensure_book_cached(req.abs_item_id)
    meta = abs_client.extract_meta(item_json)
    total = len(meta["chapters"])
    if total == 0:
        raise HTTPException(422, "Book has no chapters")

    indices = _resolve_indices(req, total)
    # Explicit user requests jump the queue; full-book bulk enqueue doesn't.
    priority = PRIORITY_USER if req.mode in ("chapter", "range") else PRIORITY_BULK

    enqueued: list[int] = []
    already_done: list[int] = []
    job_ids: list[str] = []

    for idx in indices:
        job_id = await db.upsert_job(req.abs_item_id, idx, priority=priority)
        if job_id is None:
            already_done.append(idx)
            continue
        enqueued.append(idx)
        job_ids.append(job_id)
        await queue.enqueue(job_id)

    logger.info(
        "Transcribe enqueued: book=%s enqueued=%d already_done=%d priority=%d",
        req.abs_item_id, len(enqueued), len(already_done), priority,
    )
    return {
        "book_id": req.abs_item_id,
        "enqueued": enqueued,
        "already_done": already_done,
        "job_ids": job_ids,
    }


@router.get("/jobs/active")
async def active_jobs() -> dict:
    """Currently-processing jobs across all books. Used by the Flutter
    client to show "what's being transcribed right now" in the UI.
    """
    return {"active": await db.get_active_jobs()}


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def job_status(job_id: str) -> JobStatus:
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return JobStatus(
        job_id=job["id"],
        book_id=job["book_id"],
        chapter_index=job["chapter_index"],
        status=job["status"],
        progress=float(job.get("progress", 0) or 0),
        vtt_url=(f"/api/vtt/{job['book_id']}/{job['chapter_index']}"
                 if job["status"] == "done" else None),
        error_message=job["error_message"],
    )


@router.get("/jobs/book/{abs_item_id}")
async def book_jobs(abs_item_id: str) -> dict:
    jobs = await db.get_jobs_for_book(abs_item_id)
    chapters = [
        {
            "chapter_index": j["chapter_index"],
            "status": j["status"],
            "progress": float(j.get("progress", 0) or 0),
            "priority": int(j.get("priority", 0) or 0),
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "error_message": j["error_message"],
            "vtt_url": (f"/api/vtt/{abs_item_id}/{j['chapter_index']}"
                        if j["status"] == "done" else None),
        }
        for j in jobs
    ]
    return {"book_id": abs_item_id, "chapters": chapters}
