import logging
import os
import time
import traceback

import transcription_database as db
from config import settings
from services import abs_client, ffmpeg_service, groq_client
from services.vtt_builder import build_vtt

logger = logging.getLogger("job_runner")

# Cached result of the Groq access check (one lightweight models.list() call
# per process start). Free-plan quota/auth problems then surface as an
# immediate, clear error on the job row instead of per-chunk retries.
_groq_access_ok: bool | None = None


async def _execute_job(job_id: str) -> None:
    """Run one job from start to finish. Logs every step. On any failure,
    captures the full traceback to the log file AND to the DB so the user
    can see what went wrong without having to ssh into the server.
    """
    global _groq_access_ok
    tmp_dir: str | None = None
    book_id: str | None = None
    t0 = time.monotonic()
    try:
        job = await db.get_job(job_id)
        if job is None:
            logger.warning("Job %s vanished before execution", job_id)
            return
        await db.set_job_status(job_id, "processing", progress=0)

        # 0. Validate Groq access before doing any work, so an invalid key
        #    or a free-plan rate-limit/quota block shows up immediately
        #    instead of a job hanging at 0%.
        if not _groq_access_ok:
            check = await groq_client.check_access()
            if not check["ok"]:
                raise RuntimeError(
                    f"Groq access check failed: {check['detail']}")
            _groq_access_ok = True
        book_id = job["book_id"]
        chapter_index = job["chapter_index"]
        logger.info(
            "Job %s starting: book=%s chapter=%d (priority=%s)",
            job_id, book_id, chapter_index, job.get("priority", 0),
        )

        # 1. Book metadata (cached in DB by the transcribe endpoint).
        item_json = await db.get_book(book_id)
        if item_json is None:
            logger.info("Job %s: book %s not cached, fetching from ABS",
                        job_id, book_id)
            item_json = await abs_client.get_item(book_id)
        meta = abs_client.extract_meta(item_json)
        chapters = meta["chapters"]
        if chapter_index >= len(chapters):
            raise RuntimeError(
                f"Chapter {chapter_index} out of range "
                f"(book has {len(chapters)} chapters)")
        chapter = chapters[chapter_index]
        logger.info("Job %s: chapter title=%r start=%.2f end=%.2f",
                    job_id, chapter.get("title"), chapter["start"],
                    chapter["end"])

        # 2. Map global chapter times onto audio files (handles both
        #    single-file and multi-file books).
        parts = _resolve_chapter_parts(meta["audio_files"],
                                       chapter["start"], chapter["end"])
        logger.info("Job %s: chapter spans %d audio file(s)",
                    job_id, len(parts))

        # 3. Cache the book's audio locally (once per book), then cut
        #    Groq-ready chunks (16 kHz mono) from the LOCAL copies.
        #    ffmpeg's HTTP seek hangs on large single-file books (huge
        #    `moov` boxes) where a plain HTTP download is fast — so we
        #    download via httpx and do all seeking locally. Progress 0→15%.
        tmp_dir = os.path.join(settings.TEMP_DIR, f"{book_id}_{chapter_index}")
        os.makedirs(tmp_dir, exist_ok=True)

        cache_dir = os.path.join(settings.TEMP_DIR, f"{book_id}_cache")
        files_to_cache = [(ino, abs_client.audio_file_url(book_id, ino))
                          for ino, _s, _e in parts]
        t = time.monotonic()

        async def _on_cache(frac: float) -> None:
            await db.set_job_progress(job_id, 0.08 * frac)

        await ffmpeg_service.ensure_audio_cached(
            cache_dir, files_to_cache, settings.ABS_API_TOKEN,
            progress_cb=_on_cache)
        logger.info("Job %s: audio cache ready in %.2fs (%s)",
                    job_id, time.monotonic() - t, cache_dir)
        await db.set_job_progress(job_id, 0.08)

        t = time.monotonic()
        chunk_duration = (settings.GROQ_CHUNK_SIZE_MB * 1024 * 1024
                          / (64 * 1024 / 8))
        local_parts = [
            (os.path.join(cache_dir, f"audio_{ino}"), s, e)
            for ino, s, e in parts
        ]
        chunks = await ffmpeg_service.prepare_chapter_chunks_local(
            local_parts, tmp_dir, chunk_duration, overlap_seconds=8.0)
        logger.info(
            "Job %s: prepared %d chunk(s) from %d part(s) in %.2fs",
            job_id, len(chunks), len(parts), time.monotonic() - t)
        await db.set_job_progress(job_id, 0.15)

        # 4. Transcribe chunks in parallel (bounded by MAX_CONCURRENT_CHUNKS
        #    within the chapter and MAX_CONCURRENT_GROQ globally).
        #    Progress 15% → 92%.
        async def _on_progress(fraction: float) -> None:
            await db.set_job_progress(job_id, 0.15 + fraction * 0.77)

        t = time.monotonic()
        words = await groq_client.transcribe_chunks(
            chunks, progress_cb=_on_progress,
            overlap_seconds=8.0, chunk_duration=chunk_duration)
        logger.info("Job %s: groq returned %d words in %.2fs",
                    job_id, len(words), time.monotonic() - t)

        # 5. Build VTT and write to cache.
        vtt_text = build_vtt(words)
        out_dir = os.path.join(settings.OUTPUT_DIR, book_id)
        os.makedirs(out_dir, exist_ok=True)
        vtt_path = os.path.join(out_dir, f"chapter_{chapter_index}.vtt")
        with open(vtt_path, "w", encoding="utf-8") as f:
            f.write(vtt_text)
        await db.set_job_progress(job_id, 0.98)
        logger.info("Job %s: wrote %d bytes of VTT to %s",
                    job_id, len(vtt_text), vtt_path)

        # 6. Done.
        await db.set_job_status(job_id, "done", vtt_path=vtt_path,
                                progress=100.0)
        logger.info("Job %s: DONE in %.2fs", job_id, time.monotonic() - t0)

    except Exception as e:  # noqa: BLE001 — report any failure on the job
        # Full traceback in the log file; truncated copy in the DB so the
        # Flutter UI can surface it without having to read logs.
        tb = traceback.format_exc()
        logger.error("Job %s FAILED after %.2fs: %s\n%s",
                     job_id, time.monotonic() - t0, e, tb)
        # Keep DB column reasonable in size; logs/server.log has the full thing.
        await db.set_job_status(job_id, "error",
                                error_message=f"{type(e).__name__}: {e}\n\n{tb[-3500:]}")
    finally:
        if tmp_dir is not None:
            ffmpeg_service.cleanup_tmp(tmp_dir)
        # Remove the book's audio cache once NO jobs remain for it. A failed
        # job keeps the cache (a retry reuses it instead of re-downloading).
        if book_id is not None:
            cache_dir = os.path.join(settings.TEMP_DIR, f"{book_id}_cache")
            if os.path.isdir(cache_dir):
                try:
                    remaining = await db.count_incomplete_jobs(book_id)
                except Exception:  # noqa: BLE001 — cleanup must never crash the job
                    remaining = 1
                if remaining == 0:
                    ffmpeg_service.cleanup_tmp(cache_dir)
                    logger.info("Job %s: removed audio cache %s "
                                "(book fully transcribed)", job_id, cache_dir)


def _resolve_chapter_parts(audio_files: list[dict], start: float,
                           end: float) -> list[tuple[str, float, float]]:
    """Maps global chapter times onto (ino, start_in_file, end_in_file).

    A chapter may span multiple audio files; returns one entry per file.
    """
    parts: list[tuple[str, float, float]] = []
    cursor = 0.0
    for f in audio_files:
        file_start = cursor
        file_end = cursor + f["duration"]
        cursor = file_end

        if file_end <= start or file_start >= end:
            continue

        s = max(start - file_start, 0.0)
        e = min(end - file_start, f["duration"])
        if e > s:
            parts.append((f["ino"], s, e))

    if not parts and audio_files:
        # Fallback: entire single file (shouldn't normally happen).
        f = audio_files[0]
        parts.append((f["ino"], start, end))
    return parts
