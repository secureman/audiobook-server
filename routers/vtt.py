import os

from fastapi import APIRouter, HTTPException, Response

import transcription_database as db
from config import settings

router = APIRouter()


@router.get("/vtt/{abs_item_id}/{chapter_index}")
async def get_vtt(abs_item_id: str, chapter_index: int):
    job_id = f"{abs_item_id}-{chapter_index}"
    job = await db.get_job(job_id)

    if job is None:
        # Never queued → 404 unless a cached VTT file exists anyway.
        vtt_path = os.path.join(settings.OUTPUT_DIR, abs_item_id,
                                f"chapter_{chapter_index}.vtt")
        if os.path.exists(vtt_path):
            return _serve_vtt(vtt_path)
        raise HTTPException(404, "Chapter not transcribed (never queued)")

    if job["status"] == "done":
        # The DB may hold a path from the machine that transcribed the
        # chapter (data dir moved, or DB migrated between hosts, e.g.
        # PC → Termux). Prefer the stored path when it exists on this host,
        # otherwise fall back to this install's cache location.
        vtt_path = job["vtt_path"]
        if not vtt_path or not os.path.exists(vtt_path):
            vtt_path = os.path.join(settings.OUTPUT_DIR, abs_item_id,
                                    f"chapter_{chapter_index}.vtt")
        if not os.path.exists(vtt_path):
            raise HTTPException(500, "VTT file missing")
        return _serve_vtt(vtt_path)

    if job["status"] in ("processing", "pending"):
        return Response(
            content=(
                '{"status": "processing", "progress": '
                f'{float(job.get("progress", 0) or 0)}'
                "}"
            ),
            status_code=202,
            media_type="application/json",
        )

    # error — but the on-disk VTT cache is the source of truth: if a file is
    # there (e.g. transcribed in an earlier run before the job row got
    # marked as an error), serve it instead of failing.
    vtt_path = os.path.join(settings.OUTPUT_DIR, abs_item_id,
                            f"chapter_{chapter_index}.vtt")
    if os.path.exists(vtt_path):
        return _serve_vtt(vtt_path)
    raise HTTPException(500, f"Transcription failed: {job['error_message']}")


def _serve_vtt(vtt_path: str) -> Response:
    with open(vtt_path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="text/vtt")
