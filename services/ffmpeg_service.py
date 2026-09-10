import asyncio
import logging
import os
import shutil

import httpx

from config import settings
from paths import ffmpeg_path, ffprobe_path

logger = logging.getLogger("ffmpeg")


async def extract_chapter(audio_url: str, token: str, start: float,
                          end: float, output_path: str) -> None:
    """Extracts [start, end] from an audio file into a small mono MP3.

    The input may be a remote ABS URL (adds an Authorization header) or a
    local path (no header — ffmpeg rejects `-headers` on non-HTTP inputs).
    Uses `-ss` (input seeking) + `-t` (duration). An explicit `-map 0:a:0`
    is required: ABS m4b files carry extra streams (binary chapter data,
    MJPEG cover art) and ffmpeg's auto-selection truncates the audio to
    a fraction of a second without it.
    """
    duration = max(0.0, end - start)
    cmd = [
        ffmpeg_path(), "-y",
    ]
    if "://" in audio_url:
        cmd += ["-headers", f"Authorization: Bearer {token}\r\n"]
    cmd += [
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", audio_url,
        "-map", "0:a:0",
        # libmp3lame is not compiled into every Android/Termux ffmpeg build;
        # fall back to the always-available native aac encoder when needed.
        *_audio_codec_args(),
        "-ar", "16000",
        "-ac", "1",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='ignore')[-800:]}")


async def extract_parts_concurrent(parts: list[tuple[str, float, float]],
                                   url_builder, token: str,
                                   tmp_dir: str) -> list[str]:
    """Extracts all chapter parts concurrently (they're independent jobs).

    [url_builder] maps an audio-file ino to its ABS URL. Returns the part
    file paths in the same order as [parts].
    """
    async def _one(i: int, ino: str, start: float, end: float) -> str:
        path = os.path.join(tmp_dir, f"part_{i}.mp3")
        await extract_chapter(url_builder(ino), token, start, end, path)
        return path

    return list(await asyncio.gather(
        *(_one(i, ino, s, e) for i, (ino, s, e) in enumerate(parts))
    ))


def plan_chunk_ranges(total_duration: float, chunk_duration: float,
                      overlap: float) -> list[tuple[float, float, float]]:
    """Computes (chunk_start, chunk_len, offset) ranges covering a chapter.

    offset is where the chunk (including overlap) begins in the chapter —
    this is what word timestamps get shifted by. Every chunk except the
    last is chunk_duration + overlap long (the overlap guards word
    boundaries); the last is capped by the remaining duration.
    """
    ranges: list[tuple[float, float, float]] = []
    start = 0.0
    while start < total_duration - 0.05:
        offset = max(0.0, start - (overlap if ranges else 0))
        length = min(chunk_duration + overlap, total_duration - offset)
        ranges.append((offset, length, offset))
        start += chunk_duration
    return ranges


async def prepare_chapter_chunks(parts: list[tuple[str, float, float]],
                                 url_builder, token: str, tmp_dir: str,
                                 chunk_duration: float,
                                 overlap_seconds: float = 8.0) -> list[tuple[str, float]]:
    """One-pass chapter → Groq-ready chunks (16 kHz mono, whisper-native).

    Replaces the old extract → concat → split pipeline (3 encode passes)
    with a single encode per chunk:

    * Single-part chapters (the common case): each chunk is cut straight
      from the ABS URL with `-ss`/`-t` — no intermediate file at all.
    * Multi-part chapters: parts are extracted once, concurrently, then
      chunked per part (no re-concat). Timestamps are offset by the part's
      position in the chapter.

    Returns [(chunk_path, offset_in_chapter)] in playback order.
    """
    os.makedirs(tmp_dir, exist_ok=True)
    chunks: list[tuple[str, float]] = []

    if len(parts) == 1:
        ino, start_in_file, end_in_file = parts[0]
        url = url_builder(ino)
        duration = max(0.0, end_in_file - start_in_file)
        ranges = plan_chunk_ranges(duration, chunk_duration, overlap_seconds)
        for idx, (rel_start, length, _offset) in enumerate(ranges):
            out = os.path.join(tmp_dir, f"chunk_{idx:03d}.mp3")
            await extract_chapter(url, token, start_in_file + rel_start,
                                  start_in_file + rel_start + length, out)
            chunks.append((out, rel_start))
        return chunks

    # Multi-part: extract each part once (parallel), then chunk per part.
    # Offsets must be chapter-relative: each part covers [chapter_offset,
    # chapter_offset + part_len] in the chapter, regardless of where it
    # starts inside its own file.
    part_paths = await extract_parts_concurrent(parts, url_builder, token,
                                                tmp_dir)
    idx = 0
    chapter_offset = 0.0
    for part_path, (_ino, part_start, part_end) in zip(part_paths, parts):
        part_len = max(0.0, part_end - part_start)
        ranges = plan_chunk_ranges(part_len, chunk_duration, overlap_seconds)
        for rel_start, length, _offset in ranges:
            out = os.path.join(tmp_dir, f"chunk_{idx:03d}.mp3")
            await extract_chapter(part_path, token, rel_start,
                                  rel_start + length, out)
            chunks.append((out, chapter_offset + rel_start))
            idx += 1
        chapter_offset += part_len
    return chunks


# ── Book audio cache (local download) ──────────────────────────────────
#
# ffmpeg's HTTP seek against ABS is unreliable for large single-file books:
# this repo found an 808 MB m4b whose ~10 MB `moov` box sits at ~75% into
# the file — ffmpeg hangs at 0 bytes while opening it over HTTP, even at
# -ss 0. A plain HTTP download of the same file, however, is fast and
# reliable. So each book's audio files are downloaded ONCE to a local cache
# under TEMP_DIR/<book_id>_cache, chunks are cut from the local copies
# (local ffmpeg seek is fine), and the cache is removed by job_runner once
# the book has no jobs left. Storage is checked before downloading.

_cache_locks: dict[str, asyncio.Lock] = {}
_cache_locks_guard = asyncio.Lock()


async def _get_cache_lock(cache_dir: str) -> asyncio.Lock:
    async with _cache_locks_guard:
        lock = _cache_locks.get(cache_dir)
        if lock is None:
            lock = asyncio.Lock()
            _cache_locks[cache_dir] = lock
        return lock


async def _content_length(url: str, token: str) -> int:
    """Content-Length of the ABS audio file if advertised, else 0."""
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as c:
            res = await c.head(url, headers=headers)
            return int(res.headers.get("Content-Length", 0) or 0)
    except Exception:  # noqa: BLE001 — fall back to a generic storage check
        return 0


async def _maybe_report(dl_state: dict) -> None:
    """Throttled ~2% progress pump from within a large download."""
    cb = dl_state.get("cb")
    if cb is None:
        return
    frac = min(1.0, dl_state["downloaded"]) / max(dl_state["total"], 1)
    if frac - dl_state["last"] >= 0.02 or frac >= 1.0:
        dl_state["last"] = frac
        try:
            await cb(frac)
        except Exception:  # noqa: BLE001 — progress must never break a job
            pass


async def _download_file(url: str, token: str, dst: str,
                         dl_state: dict | None = None) -> None:
    """Streams an ABS audio file to disk (chunked, so big files are fine).

    [dl_state] — mutable dict {"downloaded", "total", "last", "cb"} updated
    as bytes land so callers get smooth download progress.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    tmp = f"{dst}.part"
    timeout = httpx.Timeout(120, connect=30)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        with open(tmp, "wb") as f:
            async with c.stream("GET", url, headers=headers) as res:
                res.raise_for_status()
                async for chunk in res.aiter_bytes(262144):
                    f.write(chunk)
                    if dl_state is not None:
                        dl_state["downloaded"] += len(chunk)
                        await _maybe_report(dl_state)
    os.replace(tmp, dst)


def _check_storage(dir_path: str, needed: int) -> None:
    """Fails the download up front when there isn't enough disk space."""
    free = shutil.disk_usage(dir_path).free
    margin = max(200 * 1024 * 1024, int(needed * 0.2))
    if free < needed + margin:
        raise RuntimeError(
            f"Not enough free disk space to cache audio ({needed / 1e6:.0f} MB "
            f"needed, {free / 1e6:.0f} MB free on {dir_path!r}). "
            "Free some space or clear the TEMP_DIR cache.")


async def ensure_audio_cached(cache_dir: str,
                              files: list[tuple[str, str]],
                              token: str,
                              progress_cb=None) -> None:
    """Ensures each (ino, url) exists in the local book cache.

    Downloads missing files via HTTP (reliable where ffmpeg's HTTP seek
    hangs) with a pre-download storage check. Idempotent and safe to call
    from multiple workers at once (per-cache-dir lock).

    [progress_cb] — optional async callback(0.0–1.0) for download progress,
    throttled to ~2% steps so 800 MB files don't spam the DB.
    """
    if not files:
        return
    os.makedirs(cache_dir, exist_ok=True)
    lock = await _get_cache_lock(cache_dir)
    async with lock:
        pending: list[tuple[str, str, str, int]] = []
        for ino, url in files:
            path = os.path.join(cache_dir, f"audio_{ino}")
            if os.path.exists(path) and os.path.getsize(path) > 0:
                continue
            needed = await _content_length(url, token)
            if needed > 0:
                _check_storage(cache_dir, needed)
            if needed <= 0:
                needed = 1
            pending.append((ino, url, path, needed))

        total_bytes = sum(p[3] for p in pending)
        if not pending:
            return

        logger.info("Caching audio for book cache %s "
                    "(need %s, %d file(s))", cache_dir,
                    f"{total_bytes / 1e6:.0f} MB", len(pending))
        state = {"downloaded": 0, "total": total_bytes,
                 "last": -1.0, "cb": progress_cb}

        for ino, url, path, _needed in pending:
            await _download_file(url, token, path, state)
            await _maybe_report(state)
            logger.info("Cached %s", path)


async def prepare_chapter_chunks_local(
        parts: list[tuple[str, float, float]],
        tmp_dir: str, chunk_duration: float,
        overlap_seconds: float = 8.0) -> list[tuple[str, float]]:
    """Cut Groq-ready chunks from LOCAL audio files.

    [parts] is [(local_file_path, start_in_file, end_in_file)] pointing at
    the cached book audio. Each part is extracted to tmp once (concurrently),
    then chunked per part — same guarantees as prepare_chapter_chunks(), but
    all seeking happens on local files, which ffmpeg handles reliably.
    """
    os.makedirs(tmp_dir, exist_ok=True)

    async def _one(i: int, src: str, start: float, end: float) -> str:
        path = os.path.join(tmp_dir, f"part_{i}.mp3")
        await extract_chapter(src, "", start, end, path)
        return path

    part_paths = list(await asyncio.gather(
        *(_one(i, src, s, e) for i, (src, s, e) in enumerate(parts))
    ))
    chunks: list[tuple[str, float]] = []
    idx = 0
    chapter_offset = 0.0
    for part_path, (_src, part_start, part_end) in zip(part_paths, parts):
        part_len = max(0.0, part_end - part_start)
        ranges = plan_chunk_ranges(part_len, chunk_duration, overlap_seconds)
        for rel_start, length, _offset in ranges:
            out = os.path.join(tmp_dir, f"chunk_{idx:03d}.mp3")
            await extract_chapter(part_path, "", rel_start,
                                  rel_start + length, out)
            chunks.append((out, chapter_offset + rel_start))
            idx += 1
        chapter_offset += part_len
    return chunks


# Audio codec args. libmp3lame is missing from some ffmpeg builds (notably
# certain Android/Termux packages), so the available encoders are probed
# once at startup (see init_encoder_profile, called from the app lifespan)
# and cached here. Falls back to libmp3lame until/unless the probe says
# otherwise — every mainstream ffmpeg build for PC ships it.
_codec_args: list[str] | None = None

_MP3_ARGS = ["-c:a", "libmp3lame", "-b:a", "64k"]
_AAC_ARGS = ["-c:a", "aac", "-b:a", "64k"]


def _audio_codec_args() -> list[str]:
    return _codec_args or _MP3_ARGS


async def init_encoder_profile() -> None:
    """Probes ffmpeg's encoders once and selects MP3 or AAC accordingly."""
    global _codec_args
    if _codec_args is not None:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path(), "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        listing = out.decode(errors="ignore")
    except (OSError, FileNotFoundError):
        listing = ""
    _codec_args = _MP3_ARGS if "libmp3lame" in listing else _AAC_ARGS


async def split_audio(input_path: str, chunk_duration: float,
                      overlap: float,
                      output_dir: str) -> list[tuple[str, float]]:
    """Splits a file into chunks of ~chunk_duration seconds.

    Each chunk (except the first) includes `overlap` seconds of audio from
    the previous chunk for boundary safety.

    Returns list of (chunk_path, chunk_start_offset_in_original).
    """
    os.makedirs(output_dir, exist_ok=True)
    total = await _duration_of(input_path)

    # Probe duration via ffprobe.
    chunks: list[tuple[str, float]] = []
    for rel_start, length, offset in plan_chunk_ranges(total, chunk_duration,
                                                       overlap):
        out_path = os.path.join(output_dir, f"chunk_{len(chunks):03d}.mp3")
        cmd = [
            ffmpeg_path(), "-y",
            "-ss", f"{rel_start:.3f}",
            "-t", f"{length:.3f}",
            "-i", input_path,
            "-map", "0:a:0",
            *_audio_codec_args(),
            "-ar", "16000",
            "-ac", "1",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg split failed: {stderr.decode(errors='ignore')[-800:]}")
        chunks.append((out_path, offset))
    return chunks


async def concat_files(parts: list[str], output_path: str) -> None:
    """Concatenates audio files using ffmpeg's concat demuxer.

    Concat uses `-c copy` where possible (stream-copy, no re-encode) — but
    only if the codec args are pure copy; falls back to re-encode otherwise.
    Kept for compatibility; the hot path now uses prepare_chapter_chunks().
    """
    list_path = output_path + ".txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    cmd = [
        ffmpeg_path(), "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        *_audio_codec_args(),
        "-ar", "16000",
        "-ac", "1",
        output_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg concat failed: {stderr.decode(errors='ignore')[-800:]}")
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)


async def _duration_of(path: str) -> float:
    cmd = [
        ffprobe_path(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0


def cleanup_tmp(path: str) -> None:
    """Removes a tmp file (or every file in a tmp dir) after a job."""
    if os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        for name in os.listdir(path):
            p = os.path.join(path, name)
            if os.path.isfile(p):
                os.remove(p)
        try:
            os.rmdir(path)
        except OSError:
            pass


def ensure_dirs() -> None:
    os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    os.makedirs(settings.LOG_DIR, exist_ok=True)
