import logging

from fastapi import APIRouter, Depends, HTTPException, status

import database as db
from models import (
    BookProgress,
    BulkProgress,
    SetBookChaptersRequest,
    SetBookProgressRequest,
    SetFinishedRequest,
    SetPositionRequest,
)
from security import get_current_user

logger = logging.getLogger("router.progress")

router = APIRouter()


def _book_row_to_model(row: dict) -> BookProgress:
    return BookProgress(
        abs_item_id=row["abs_item_id"],
        is_finished=bool(row.get("is_finished", 0)),
        last_chapter_index=row.get("last_chapter_index"),
        last_position_seconds=row.get("last_position_seconds"),
        progress=row.get("progress"),
        updated_at=row.get("updated_at"),
    )


@router.get("/progress", response_model=BulkProgress)
async def get_bulk_progress(
    user: dict = Depends(get_current_user),
) -> BulkProgress:
    """Bulk fetch on login.

    Three small SELECTs (book_progress, chapter_done, chapter_position)
    keyed by user_id. The Flutter client uses this to populate
    ReadChaptersController, ChapterPositionsController and
    BookPlaybackProgress in one round trip.

    Chapter indices come back as `dict[str, dict[int, float]]` for
    positions and `dict[str, list[int]]` for done — keyed by abs_item_id
    so the client can look up by the same id it uses everywhere else.
    """
    user_id = user["id"]

    book_rows = await db.get_all_book_progress(user_id)
    done_rows = await db.get_all_chapters_done(user_id)
    pos_rows = await db.get_all_chapter_positions(user_id)

    books = {r["abs_item_id"]: _book_row_to_model(r) for r in book_rows}
    chapters_done: dict[str, list[int]] = {}
    for abs_item_id, chapter_index in done_rows:
        chapters_done.setdefault(abs_item_id, []).append(chapter_index)
    chapter_positions: dict[str, dict[int, float]] = {}
    for abs_item_id, chapter_index, pos in pos_rows:
        chapter_positions.setdefault(abs_item_id, {})[chapter_index] = pos

    logger.info(
        "Bulk progress fetch: user=%s books=%d done_groups=%d pos_groups=%d",
        user_id, len(books), len(chapters_done), len(chapter_positions),
    )
    return BulkProgress(
        books=books,
        chapters_done=chapters_done,
        chapter_positions=chapter_positions,
    )


@router.get("/progress/book/{abs_item_id}", response_model=BookProgress)
async def get_book(
    abs_item_id: str,
    user: dict = Depends(get_current_user),
) -> BookProgress:
    row = await db.get_book_progress(user["id"], abs_item_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No progress recorded for this book",
        )
    return _book_row_to_model(row)


@router.post("/progress/book/{abs_item_id}/finished",
             response_model=BookProgress)
async def set_book_finished(
    abs_item_id: str,
    req: SetFinishedRequest,
    user: dict = Depends(get_current_user),
) -> BookProgress:
    row = await db.upsert_book_finished(
        user["id"], abs_item_id, req.is_finished
    )
    logger.info(
        "Book finished: user=%s book=%s is_finished=%s",
        user["id"], abs_item_id, req.is_finished,
    )
    return _book_row_to_model(row)


@router.put("/progress/book/{abs_item_id}", response_model=BookProgress)
async def set_book_progress(
    abs_item_id: str,
    req: SetBookProgressRequest,
    user: dict = Depends(get_current_user),
) -> BookProgress:
    """Full book-level upsert from the player's rolling sync — the direct
    replacement for the old ABS `/api/me/progress` PATCH. Keeps the
    continue-reading bookmark and the whole-book fraction fresh in one
    round trip.
    """
    row = await db.upsert_book_progress(
        user["id"],
        abs_item_id,
        req.last_chapter_index,
        req.last_position_seconds,
        req.progress,
    )
    logger.info(
        "Book progress: user=%s book=%s chapter=%d pos=%.1f frac=%s",
        user["id"], abs_item_id, req.last_chapter_index,
        req.last_position_seconds, req.progress,
    )
    return _book_row_to_model(row)


@router.put("/progress/book/{abs_item_id}/chapters",
            status_code=status.HTTP_204_NO_CONTENT)
async def replace_book_chapters(
    abs_item_id: str,
    req: SetBookChaptersRequest,
    user: dict = Depends(get_current_user),
) -> None:
    """Replaces the book's whole chapter_done set (whole-book listened
    toggle). One round trip instead of N per-chapter calls.
    """
    if any(c < 0 for c in req.chapters):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="chapter indexes must be >= 0",
        )
    await db.replace_book_chapters(user["id"], abs_item_id, req.chapters)
    logger.info(
        "Replace book chapters: user=%s book=%s count=%d",
        user["id"], abs_item_id, len(req.chapters),
    )
    return None


@router.post("/progress/chapter/{abs_item_id}/{chapter_index}/done",
             status_code=status.HTTP_204_NO_CONTENT)
async def mark_chapter_done(
    abs_item_id: str,
    chapter_index: int,
    user: dict = Depends(get_current_user),
) -> None:
    if chapter_index < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="chapter_index must be >= 0",
        )
    await db.mark_chapter_done(user["id"], abs_item_id, chapter_index)
    logger.info(
        "Chapter marked done: user=%s book=%s chapter=%d",
        user["id"], abs_item_id, chapter_index,
    )
    return None


@router.delete("/progress/chapter/{abs_item_id}/{chapter_index}/done",
               status_code=status.HTTP_204_NO_CONTENT)
async def unmark_chapter_done(
    abs_item_id: str,
    chapter_index: int,
    user: dict = Depends(get_current_user),
) -> None:
    if chapter_index < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="chapter_index must be >= 0",
        )
    await db.unmark_chapter_done(user["id"], abs_item_id, chapter_index)
    logger.info(
        "Chapter unmarked: user=%s book=%s chapter=%d",
        user["id"], abs_item_id, chapter_index,
    )
    return None


@router.put("/progress/chapter/{abs_item_id}/{chapter_index}/position",
            status_code=status.HTTP_204_NO_CONTENT)
async def set_chapter_position(
    abs_item_id: str,
    chapter_index: int,
    req: SetPositionRequest,
    user: dict = Depends(get_current_user),
) -> None:
    if chapter_index < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="chapter_index must be >= 0",
        )
    await db.upsert_chapter_position(
        user["id"], abs_item_id, chapter_index, req.position_seconds
    )
    # Also keep the 'last played' bookmark on the book row so the bulk
    # fetch / 'continue reading' surfaces the freshest position even if
    # the per-chapter table is older. One tiny write, one user-visible
    # improvement.
    await db.upsert_book_position(
        user["id"], abs_item_id, chapter_index, req.position_seconds
    )
    return None