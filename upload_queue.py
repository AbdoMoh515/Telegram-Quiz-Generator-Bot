# upload_queue.py
#
# Global FIFO queue for ALL .txt/.md Telegram DOCUMENT uploads.
#
# Only ``MAX_WORKERS`` (two) uploads download/parse at the same time, no
# matter how many users upload concurrently. Everything else waits its turn
# in a single process-wide FIFO queue. Queue entries are tiny metadata
# records (job id, user/chat ids, Telegram file id, file name) -- never full
# documents -- and parsed question records live on disk (JSONL) from parse
# through preview, dispatch and Show-as-Text. FSM state only ever holds
# small identifiers (job id / token / counts).
#
# This module is importable WITHOUT aiogram installed (offline tests): the
# bot object, the preview keyboard and the FSM factory are injected by the
# caller (main.py) or by tests.

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from utils import (
    UPLOAD_RESULT_TTL_SECONDS,
    build_empty_result_text,
    build_preview_text,
    iter_disk_questions,
    load_preview_sample,
    parse_upload_file_to_disk,
)

logger = logging.getLogger(__name__)

#: At most this many uploads download/parse concurrently, globally.
MAX_WORKERS = 2

#: Manifest statuses. Terminal ones are removed by age-based purge.
STATUS_QUEUED = "queued"
STATUS_ACTIVE = "active"
STATUS_PREVIEW = "preview"
STATUS_CONSUMED = "consumed"      # claimed by Send/Cancel, being finished
STATUS_DISPATCHED = "dispatched"
STATUS_DONE = "done"          # parsed, but nothing valid to preview
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

_TERMINAL_STATUSES = {
    STATUS_DISPATCHED, STATUS_DONE, STATUS_CANCELLED, STATUS_FAILED,
    STATUS_EXPIRED,
}
#: Stale non-queued jobs removed by age-based purge (preview included: a
#: preview nobody confirmed within the TTL is dead weight on disk).
_PURGEABLE_STATUSES = _TERMINAL_STATUSES | {
    STATUS_PREVIEW, STATUS_ACTIVE, STATUS_CONSUMED}


def base_dir() -> str:
    """Root for all queue/disk state (honours BOT_TEMP_DIR for tests)."""
    return os.environ.get("BOT_TEMP_DIR", "temp")


def uploads_root(base: Optional[str] = None) -> str:
    return os.path.join(base or base_dir(), "uploads")


def jobs_root(base: Optional[str] = None) -> str:
    return os.path.join(uploads_root(base), "jobs")


def job_dir(job_id: str, base: Optional[str] = None) -> str:
    return os.path.join(jobs_root(base), job_id)


def questions_path(job_id: str, base: Optional[str] = None) -> str:
    return os.path.join(job_dir(job_id, base), "questions.jsonl")


def skipped_path(job_id: str, base: Optional[str] = None) -> str:
    return os.path.join(job_dir(job_id, base), "skipped.jsonl")


def source_path(job_id: str, ext: str = ".txt",
                base: Optional[str] = None) -> str:
    return os.path.join(job_dir(job_id, base), f"source{ext}")


# ---------------------------------------------------------------------------
# Module-level runtime state (resettable for tests)
# ---------------------------------------------------------------------------

_queue: Optional[asyncio.Queue] = None
_slots = asyncio.Semaphore(MAX_WORKERS)
_claim_lock: Optional[asyncio.Lock] = None
_registry: Dict[str, str] = {}          # preview token -> job_id
_dead_tokens: Dict[str, str] = {}       # spent preview token -> DC reason
_dead_order: List[str] = []             # insertion order for bounding
_active_count = 0                        # currently downloading/parsing
_peak_active = 0                         # high-water mark (tests/diagnostics)
_start_order: List[str] = []             # job start order (tests/diagnostics)

#: Why a preview token is dead (already claimed / result expired+cleaned).
DEAD_CONSUMED = "consumed"
DEAD_EXPIRED = "expired"
_MAX_DEAD_TOKENS = 2048


def _remember_dead(token: str, reason: str) -> None:
    if token not in _dead_tokens:
        _dead_order.append(token)
        while len(_dead_order) > _MAX_DEAD_TOKENS:
            _dead_tokens.pop(_dead_order.pop(0), None)
    _dead_tokens[token] = reason


def dead_token_reason(token: str) -> Optional[str]:
    """Why *token* is dead (None when live or never issued)."""
    return _dead_tokens.get(token)


def _get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def _get_claim_lock() -> asyncio.Lock:
    global _claim_lock
    if _claim_lock is None:
        _claim_lock = asyncio.Lock()
    return _claim_lock


def reset_for_tests() -> None:
    """Drop all runtime state (tests only; the live bot never calls this)."""
    global _queue, _claim_lock, _registry, _active_count, _peak_active, \
        _start_order, _slots, _dead_tokens, _dead_order
    _queue = None
    _claim_lock = None
    _registry = {}
    _dead_tokens = {}
    _dead_order = []
    _active_count = 0
    _peak_active = 0
    _start_order = []
    _slots = asyncio.Semaphore(MAX_WORKERS)


def active_count() -> int:
    return _active_count


def peak_active() -> int:
    return _peak_active


def pending_count() -> int:
    q = _queue
    return q.qsize() if q is not None else 0


# ---------------------------------------------------------------------------
# Manifest helpers (tiny JSON; written atomically via temp + rename)
# ---------------------------------------------------------------------------

def _manifest_path(job_dir_path: str) -> str:
    return os.path.join(job_dir_path, "manifest.json")


def _write_manifest_sync(job_dir_path: str, manifest: Dict[str, Any]) -> None:
    os.makedirs(job_dir_path, exist_ok=True)
    tmp = os.path.join(job_dir_path, "manifest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _manifest_path(job_dir_path))


def _load_manifest_sync(job_dir_path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_manifest_path(job_dir_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


async def _save_manifest(job_dir_path: str, manifest: Dict[str, Any]) -> None:
    manifest["updated_at"] = time.time()
    await asyncio.to_thread(_write_manifest_sync, job_dir_path, manifest)


def get_job(job_id: str, base: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load a job manifest (sync; fine for small JSON outside the loop)."""
    return _load_manifest_sync(job_dir(job_id, base))


def job_files_exist(job_id: str, base: Optional[str] = None) -> bool:
    return os.path.exists(questions_path(job_id, base))


def delete_job(job_id: str, base: Optional[str] = None) -> None:
    """Remove a job directory (sync; call via to_thread from async code)."""
    shutil.rmtree(job_dir(job_id, base), ignore_errors=True)


async def _delete_job_async(job_id: str,
                            base: Optional[str] = None) -> None:
    await asyncio.to_thread(delete_job, job_id, base)


# ---------------------------------------------------------------------------
# Enqueue (metadata only -- the handler never downloads)
# ---------------------------------------------------------------------------

async def enqueue_upload(user_id: int, chat_id: int, file_id: str,
                         file_name: str,
                         base: Optional[str] = None) -> Tuple[str, int]:
    """Queue an upload; returns ``(job_id, queue_position)``."""
    root = jobs_root(base)
    await asyncio.to_thread(os.makedirs, root, exist_ok=True)
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    manifest = {
        "job_id": job_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "file_id": file_id,
        "file_name": file_name or "upload",
        "status": STATUS_QUEUED,
        "created_at": now,
        "updated_at": now,
        "token": None,
        "valid_count": 0,
        "skipped_count": 0,
        "preview_message_id": None,
        "error": None,
    }
    await asyncio.to_thread(
        _write_manifest_sync, job_dir(job_id, base), manifest)
    _get_queue().put_nowait(job_id)
    position = _get_queue().qsize()
    logger.info(f"Enqueued upload {job_id} ({file_name}) at position {position}.")
    # Opportunistic age-based cleanup so abandoned results cannot fill disk.
    await asyncio.to_thread(purge_expired, None, base)
    return job_id, position


def requeue_job(job_id: str) -> None:
    """Put an existing (spooled) job back on the FIFO (startup recovery)."""
    _get_queue().put_nowait(job_id)


# ---------------------------------------------------------------------------
# Workers: at most MAX_WORKERS download/parse concurrently, globally
# ---------------------------------------------------------------------------

Presenter = Callable[..., Awaitable[Any]]
FsmFactory = Callable[[int, int], Any]


async def _default_presenter(bot: Any, chat_id: int, text: str,
                             token: str) -> Any:
    from keyboards import get_preview_keyboard  # lazy: needs aiogram
    return await bot.send_message(
        chat_id, text, reply_markup=get_preview_keyboard(token),
        parse_mode="HTML")


def _download_error_text(exc: BaseException) -> str:
    msg = str(exc or "").lower()
    if "too big" in msg or "too large" in msg or "file size" in msg:
        return ("❌ Telegram refused the download: bots can only download "
                "files up to 20 MB. Please split the file and try again.")
    if isinstance(exc, OSError):
        import errno
        if exc.errno in (errno.ENOSPC, 28):
            return ("❌ The bot's disk is full, so your file could not be "
                    "processed. Please try again later.")
        return f"❌ Could not save your file (disk error: {exc})."
    return f"❌ Could not download your file ({exc}). Please try again."


def _parse_error_text(exc: BaseException) -> str:
    """User-facing notice for a download-OK but parse/process failure."""
    if isinstance(exc, OSError):
        import errno
        if exc.errno in (errno.ENOSPC, 28):
            return ("❌ The bot's disk is full, so your file could not be "
                    "processed. Please try again later.")
        return f"❌ Could not process your file (disk error: {exc})."
    return f"❌ Could not process your file ({exc}). Please try again."


async def _set_fsm_preview(fsm_factory: Optional[FsmFactory],
                           awaiting_state: Any,
                           manifest: Dict[str, Any]) -> None:
    if fsm_factory is None:
        return
    try:
        state = fsm_factory(manifest["chat_id"], manifest["user_id"])
        await state.set_state(
            awaiting_state if awaiting_state is not None
            else "AWAITING_CONFIRMATION")
        await state.update_data(
            preview_job_id=manifest["job_id"],
            preview_token=manifest["token"],
            preview_valid_count=manifest["valid_count"],
            preview_skipped_count=manifest["skipped_count"],
            extracted_job_id=manifest["job_id"],
        )
    except Exception as e:
        # Preview delivery matters more than FSM mirroring; the token
        # registry is the source of truth for confirm/cancel.
        logger.warning(f"Could not mirror preview state to FSM: {e}")


async def _process_job(bot: Any, job_id: str,
                       fsm_factory: Optional[FsmFactory] = None,
                       presenter: Optional[Presenter] = None,
                       awaiting_state: Any = None,
                       base: Optional[str] = None) -> None:
    global _active_count, _peak_active
    directory = job_dir(job_id, base)
    manifest = await asyncio.to_thread(_load_manifest_sync, directory)
    if manifest is None:
        logger.warning(f"Upload job {job_id} vanished before processing.")
        return
    if manifest.get("status") not in (STATUS_QUEUED, STATUS_ACTIVE):
        return  # already handled (e.g. requeued twice)
    manifest["status"] = STATUS_ACTIVE
    await _save_manifest(directory, manifest)

    present = presenter or _default_presenter
    ext = os.path.splitext(manifest.get("file_name") or "")[1].lower() or ".txt"
    src = source_path(job_id, ext, base)

    # The worker slot is held for download + parse only: preview delivery
    # and user confirmation happen outside it so slow users never block
    # the queue.
    async with _slots:
        _active_count += 1
        _peak_active = max(_peak_active, _active_count)
        _start_order.append(job_id)
        try:
            try:
                await bot.download(manifest["file_id"], destination=src)
            except Exception as e:
                logger.error(f"Download failed for job {job_id}: {e}",
                             exc_info=True)
                manifest["status"] = STATUS_FAILED
                manifest["error"] = str(e)[:500]
                await _save_manifest(directory, manifest)
                try:
                    await bot.send_message(
                        manifest["chat_id"], _download_error_text(e))
                except Exception:
                    logger.warning(f"Could not notify chat "
                                   f"{manifest['chat_id']} of failure.")
                await _delete_job_async(job_id, base)
                return
            try:
                try:
                    summary = await asyncio.to_thread(
                        parse_upload_file_to_disk, src, directory)
                except Exception as e:
                    # A streaming-parse failure (disk-full, decoding/read
                    # error, ...): notify clearly, drop the partial spool
                    # and return normally so the worker keeps serving the
                    # rest of the FIFO instead of leaving the user waiting.
                    logger.error(f"Parse failed for job {job_id}: {e}",
                                 exc_info=True)
                    manifest["status"] = STATUS_FAILED
                    manifest["error"] = str(e)[:500]
                    await _save_manifest(directory, manifest)
                    try:
                        await bot.send_message(
                            manifest["chat_id"], _parse_error_text(e))
                    except Exception:
                        logger.warning(f"Could not notify chat "
                                       f"{manifest['chat_id']} of failure.")
                    await _delete_job_async(job_id, base)
                    return
            finally:
                try:
                    os.remove(src)
                except OSError:
                    pass
            manifest["valid_count"] = summary["valid_count"]
            manifest["skipped_count"] = summary["skipped_count"]
        finally:
            _active_count -= 1

    if manifest["valid_count"] <= 0:
        manifest["status"] = STATUS_DONE
        await _save_manifest(directory, manifest)
        try:
            _, sample_skipped = await asyncio.to_thread(
                load_preview_sample,
                questions_path(job_id, base), skipped_path(job_id, base))
            text = build_empty_result_text(
                sample_skipped,
                total_skipped=manifest["skipped_count"])
            await bot.send_message(manifest["chat_id"], text,
                                   parse_mode="HTML")
        except Exception:
            logger.warning(f"Could not notify chat {manifest['chat_id']} "
                           f"of empty result.")
        await _delete_job_async(job_id, base)
        return

    token = secrets.token_hex(8)
    manifest["token"] = token
    manifest["status"] = STATUS_PREVIEW
    await _save_manifest(directory, manifest)
    _registry[token] = job_id
    try:
        sample_q, sample_s = await asyncio.to_thread(
            load_preview_sample,
            questions_path(job_id, base), skipped_path(job_id, base))
        text = build_preview_text(
            sample_q, sample_s,
            total_valid=manifest["valid_count"],
            total_skipped=manifest["skipped_count"])
        sent = await present(bot, manifest["chat_id"], text, token)
        manifest["preview_message_id"] = getattr(sent, "message_id", None)
        await _save_manifest(directory, manifest)
    except Exception as e:
        logger.error(f"Preview delivery failed for job {job_id}: {e}",
                     exc_info=True)
        _registry.pop(token, None)
        manifest["status"] = STATUS_FAILED
        manifest["error"] = str(e)[:500]
        await _save_manifest(directory, manifest)
        try:
            await bot.send_message(
                manifest["chat_id"],
                "❌ Your file was parsed but the preview could not be "
                "delivered. Please try again.")
        except Exception:
            pass
        await _delete_job_async(job_id, base)
        return
    await _set_fsm_preview(fsm_factory, awaiting_state, manifest)
    logger.info(f"Upload job {job_id} ready for preview "
                f"({manifest['valid_count']} valid).")


async def process_one(bot: Any, *,
                      fsm_factory: Optional[FsmFactory] = None,
                      presenter: Optional[Presenter] = None,
                      awaiting_state: Any = None,
                      base: Optional[str] = None) -> Optional[str]:
    """Process the next queued upload (used by workers and tests)."""
    job_id = await _get_queue().get()
    try:
        await _process_job(bot, job_id, fsm_factory, presenter,
                           awaiting_state, base)
        return job_id
    finally:
        _get_queue().task_done()


async def _worker_loop(bot: Any, *,
                       fsm_factory: Optional[FsmFactory] = None,
                       presenter: Optional[Presenter] = None,
                       awaiting_state: Any = None,
                       base: Optional[str] = None) -> None:
    while True:
        try:
            await process_one(bot, fsm_factory=fsm_factory,
                              presenter=presenter,
                              awaiting_state=awaiting_state, base=base)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Upload worker error: {e}", exc_info=True)


def start_workers(bot: Any, *, count: int = MAX_WORKERS,
                  fsm_factory: Optional[FsmFactory] = None,
                  presenter: Optional[Presenter] = None,
                  awaiting_state: Any = None,
                  base: Optional[str] = None) -> List[asyncio.Task]:
    """Start *count* FIFO consumer tasks (two in production)."""
    return [asyncio.create_task(
        _worker_loop(bot, fsm_factory=fsm_factory, presenter=presenter,
                     awaiting_state=awaiting_state, base=base),
        name=f"upload-worker-{i}") for i in range(count)]


# ---------------------------------------------------------------------------
# Preview token registry (confirm/cancel claim exactly once)
# ---------------------------------------------------------------------------

async def consume_upload_token(token: str,
                               base: Optional[str] = None
                               ) -> Optional[Dict[str, Any]]:
    """Atomically claim a preview token; None if stale/already consumed."""
    async with _get_claim_lock():
        job_id = _registry.pop(token, None)
        if job_id is None:
            return None
        manifest = await asyncio.to_thread(
            _load_manifest_sync, job_dir(job_id, base))
        if manifest is None or manifest.get("status") != STATUS_PREVIEW:
            return None
        if manifest.get("token") != token:
            return None
        if not job_files_exist(job_id, base):
            manifest["status"] = STATUS_EXPIRED
            await _save_manifest(job_dir(job_id, base), manifest)
            _remember_dead(token, DEAD_EXPIRED)
            return None
        manifest["status"] = STATUS_CONSUMED
        await _save_manifest(job_dir(job_id, base), manifest)
        _remember_dead(token, DEAD_CONSUMED)
        manifest["_job_id"] = job_id
        return manifest


async def mark_upload_dispatched(job_id: str,
                                 base: Optional[str] = None) -> None:
    """Record a successful dispatch; result files stay for Show-as-Text."""
    directory = job_dir(job_id, base)
    manifest = await asyncio.to_thread(_load_manifest_sync, directory)
    if manifest is None:
        return
    manifest["status"] = STATUS_DISPATCHED
    await _save_manifest(directory, manifest)


def peek_upload_token(token: str) -> Optional[str]:
    """Return the job id for a live preview token without claiming it."""
    return _registry.get(token)


async def describe_upload_token(token: str,
                                base: Optional[str] = None
                                ) -> Optional[Dict[str, Any]]:
    """Return ownership info for a live preview token without claiming it.

    Returns ``{"job_id", "user_id", "chat_id"}`` when *token* is currently
    consumable, else None (unknown, stale or already-consumed tokens).
    Lets callback handlers verify the tapping user/chat owns the preview
    BEFORE :func:`consume_upload_token`, so foreign presses can be
    refused without consuming or dispatching anything.
    """
    job_id = _registry.get(token)
    if job_id is None:
        return None
    manifest = await asyncio.to_thread(
        _load_manifest_sync, job_dir(job_id, base))
    if manifest is None or manifest.get("status") != STATUS_PREVIEW:
        return None
    if manifest.get("token") != token:
        return None
    return {"job_id": job_id,
            "user_id": manifest.get("user_id"),
            "chat_id": manifest.get("chat_id")}


async def mark_upload_cancelled(job_id: str,
                                base: Optional[str] = None) -> None:
    """Delete a cancelled job's disk state (best effort)."""
    await _delete_job_async(job_id, base)


# ---------------------------------------------------------------------------
# Startup recovery + age-based purge
# ---------------------------------------------------------------------------

def recover_spool(base: Optional[str] = None
                  ) -> Tuple[List[Dict[str, Any]], int]:
    """Reconcile on-disk jobs after a (re)start. Sync; run via to_thread.

    Returns ``(to_requeue, expired_cleaned)``: manifests with status
    queued/active go back on the FIFO (sorted oldest-first for fairness);
    live previews keep their files and their tokens are re-registered by
    :func:`reregister_previews` so stale buttons keep working; anything
    else is marked expired and removed.
    """
    root = jobs_root(base)
    to_requeue: List[Dict[str, Any]] = []
    cleaned = 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return [], 0
    for name in names:
        directory = os.path.join(root, name)
        manifest = _load_manifest_sync(directory)
        if manifest is None:
            shutil.rmtree(directory, ignore_errors=True)
            cleaned += 1
            continue
        status = manifest.get("status")
        if status in (STATUS_QUEUED, STATUS_ACTIVE):
            manifest["status"] = STATUS_QUEUED
            manifest["updated_at"] = time.time()
            _write_manifest_sync(directory, manifest)
            to_requeue.append(manifest)
        elif status == STATUS_PREVIEW and os.path.exists(
                os.path.join(directory, "questions.jsonl")):
            continue  # kept; token re-registered below
        else:
            shutil.rmtree(directory, ignore_errors=True)
            cleaned += 1
    to_requeue.sort(key=lambda m: m.get("created_at", 0))
    return to_requeue, cleaned


def reregister_previews(base: Optional[str] = None) -> int:
    """Rebuild the token registry for surviving previews (startup)."""
    root = jobs_root(base)
    count = 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return 0
    for name in names:
        manifest = _load_manifest_sync(os.path.join(root, name))
        if (manifest and manifest.get("status") == STATUS_PREVIEW
                and manifest.get("token")):
            _registry[manifest["token"]] = manifest["job_id"]
            count += 1
    return count


def purge_expired(max_age: Optional[float] = None,
                  base: Optional[str] = None) -> int:
    """Delete spooled jobs older than *max_age* (default TTL). Sync."""
    limit = UPLOAD_RESULT_TTL_SECONDS if max_age is None else max_age
    now = time.time()
    root = jobs_root(base)
    removed = 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return 0
    for name in names:
        directory = os.path.join(root, name)
        manifest = _load_manifest_sync(directory)
        if manifest is None:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
            continue
        if manifest.get("status") == STATUS_QUEUED:
            continue  # never drop work the user is still waiting for
        updated = manifest.get("updated_at")
        if updated is None:
            updated = manifest.get("created_at", 0)
        if (manifest.get("status") in _PURGEABLE_STATUSES
                and now - updated > limit):
            for token, jid in list(_registry.items()):
                if jid == manifest.get("job_id"):
                    _registry.pop(token, None)
            token = manifest.get("token")
            if token:
                _dead_tokens.pop(token, None)
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed
