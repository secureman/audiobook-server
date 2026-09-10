"""Durable transcription job queue.

Jobs are stored in SQLite (status `pending`) and picked up by a persistent
worker coroutine that runs for the entire lifetime of the server — not tied
to any HTTP request. This makes transcription survive:
  * the client closing / backgrounding the app
  * no in-flight request keeping the task alive
  * process health, since stuck `processing` rows are reset to `pending`
    on startup so they are re-queued.

Concurrency is bounded by MAX_CONCURRENT_JOBS worker tasks that share the
Groq semaphore.
"""

import asyncio
import logging

import transcription_database as db
from config import settings
from services import job_runner

logger = logging.getLogger("queue")

# Job ids in flight across all workers (dedupe enqueue calls).
_inflight: set[str] = set()

# One worker task per concurrent job slot.
_workers: list[asyncio.Task] = []


async def _claim_job() -> str | None:
    return await db.claim_job()


async def _worker() -> None:
    """Loops, claiming and running pending jobs until told to stop.

    Bounded by MAX_CONCURRENT_JOBS workers (CPU-bound ffmpeg work). Groq
    API concurrency is bounded separately inside groq_client — no shared
    semaphore here anymore.
    """
    while True:
        job_id = await _claim_job()
        if job_id is None:
            await asyncio.sleep(1.0)
            continue
        _inflight.add(job_id)
        logger.info("Worker picked up job %s (in-flight: %d)",
                    job_id, len(_inflight))
        try:
            await job_runner._execute_job(job_id)
        except Exception as e:  # noqa: BLE001
            # _execute_job already catches its own exceptions, so this only
            # fires for truly unexpected errors (e.g. programming bugs).
            logger.exception("Unhandled error in job %s: %s", job_id, e)
            try:
                await db.set_job_status(job_id, "error", error_message=str(e))
            except Exception:  # noqa: BLE001
                logger.exception("Also failed to mark job %s as errored", job_id)
        finally:
            _inflight.discard(job_id)


async def start_workers() -> None:
    """Launches the persistent worker pool (idempotent)."""
    if _workers:
        return
    for _ in range(max(1, settings.MAX_CONCURRENT_JOBS)):
        _workers.append(asyncio.create_task(_worker()))


async def stop_workers() -> None:
    for w in _workers:
        w.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()


async def enqueue(job_id: str) -> None:
    """Add a job to the queue.

    The durable worker pool picks up any `pending` row on its next poll, so
    enqueue just needs to exist (it keeps a wake handle if we ever switch to
    an asyncio.Queue). Kept as a semantic hook for the routers.
    """
    # Wake a worker immediately instead of waiting up to 1s for its poll.
    for w in _workers:
        if not w.done():
            # A tiny yield so the worker re-polls promptly.
            await asyncio.sleep(0)
            break


async def ensure_started_on_boot() -> None:
    """Resets rows stuck in 'processing' (from a previous crash/restart)
    back to 'pending' so they get picked up again."""
    reset = await db.reset_stuck_processing()
    logger.info("Reset %d stuck 'processing' rows to 'pending'",
                reset if isinstance(reset, int) else 0)
    await start_workers()
    logger.info("Worker pool started: %d concurrent slot(s)",
                max(1, settings.MAX_CONCURRENT_JOBS))