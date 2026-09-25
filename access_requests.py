# access_requests.py
#
# Pending bot-access requests from unapproved users.
#
# State lives in a small stdlib-SQLite database under the ignored temp/
# directory (``BOT_TEMP_DIR`` honoured for tests) -- never in the tracked
# users.json / allowed_users.json files, which are only written by live
# admin allow/remove actions. Decisions are atomic compare-and-set updates
# (``WHERE status='pending'``), so two admins tapping Approve/Reject at the
# same time cannot both win: exactly one UPDATE affects a row.

import asyncio
import html
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

#: Pending requests older than this stop blocking re-requests and may be
#: swept (repeats of /start while pending never re-notify admins anyway).
REQUEST_TTL_SECONDS = 7 * 24 * 3600

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_requests (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    decided_by INTEGER,
    admin_messages TEXT NOT NULL DEFAULT '[]'
)
"""


def db_path(base: Optional[str] = None) -> str:
    root = base or os.environ.get("BOT_TEMP_DIR", "temp")
    return os.path.join(root, "access_requests.db")


def _connect(base: Optional[str] = None) -> sqlite3.Connection:
    root = base or os.environ.get("BOT_TEMP_DIR", "temp")
    os.makedirs(root, exist_ok=True)
    conn = sqlite3.connect(db_path(base), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    try:
        data["admin_messages"] = json.loads(data.get("admin_messages") or "[]")
    except ValueError:
        data["admin_messages"] = []
    return data


def expire_old_requests(max_age: float = REQUEST_TTL_SECONDS,
                        base: Optional[str] = None) -> int:
    """Mark stale pending requests expired; returns rows expired."""
    cutoff = time.time() - max_age
    with _lock:
        conn = _connect(base)
        try:
            cur = conn.execute(
                "UPDATE access_requests SET status=?, updated_at=? "
                "WHERE status=? AND created_at < ?",
                (STATUS_EXPIRED, time.time(), STATUS_PENDING, cutoff))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def request_access(user_id: int, username: str, full_name: str,
                   base: Optional[str] = None) -> str:
    """Record an access request; returns its disposition.

    ``"created"`` -- brand-new (or re-activated) pending request: the caller
    should notify admins. ``"pending"`` -- a live request already exists
    (repeat /start): notify nobody (anti-spam). ``"approved"`` /
    ``"rejected"`` -- already decided: notify nobody.
    """
    username = username or ""
    full_name = full_name or ""
    now = time.time()
    with _lock:
        conn = _connect(base)
        try:
            conn.execute(
                "UPDATE access_requests SET status=?, updated_at=? "
                "WHERE status=? AND created_at < ?",
                (STATUS_EXPIRED, now, STATUS_PENDING,
                 now - REQUEST_TTL_SECONDS))
            row = conn.execute(
                "SELECT * FROM access_requests WHERE user_id = ?",
                (user_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO access_requests "
                    "(user_id, username, full_name, status, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, username, full_name, STATUS_PENDING, now, now))
                conn.commit()
                return "created"
            data = _row_to_dict(row)
            assert data is not None
            if data["status"] == STATUS_PENDING:
                # Refresh the stored name in case it changed; the request
                # itself stays single (no duplicate admin notification).
                conn.execute(
                    "UPDATE access_requests SET username=?, full_name=?, "
                    "updated_at=? WHERE user_id=?",
                    (username, full_name, now, user_id))
                conn.commit()
                return "pending"
            if data["status"] == STATUS_EXPIRED:
                conn.execute(
                    "UPDATE access_requests SET username=?, full_name=?, "
                    "status=?, created_at=?, updated_at=?, decided_by=NULL "
                    "WHERE user_id=?",
                    (username, full_name, STATUS_PENDING, now, now, user_id))
                conn.commit()
                return "created"
            return str(data["status"])
        finally:
            conn.close()


def get_request(user_id: int,
                base: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect(base)
        try:
            row = conn.execute(
                "SELECT * FROM access_requests WHERE user_id = ?",
                (user_id,)).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()


def resolve_request(user_id: int, decision: str, decided_by: int,
                    base: Optional[str] = None
                    ) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Atomically resolve a pending request.

    Returns ``("resolved", row)`` when this call won the race,
    ``("already", row)`` when it was decided earlier (idempotent repeat /
    lost race), ``("missing", None)`` when no request exists (stale click).
    *decision* must be ``"approved"`` or ``"rejected"``.
    """
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        raise ValueError(f"Bad decision: {decision!r}")
    now = time.time()
    with _lock:
        conn = _connect(base)
        try:
            cur = conn.execute(
                "UPDATE access_requests SET status=?, updated_at=?, "
                "decided_by=? WHERE user_id=? AND status=?",
                (decision, now, decided_by, user_id, STATUS_PENDING))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM access_requests WHERE user_id = ?",
                (user_id,)).fetchone()
            data = _row_to_dict(row)
            if cur.rowcount == 1:
                return "resolved", data
            if data is None:
                return "missing", None
            return "already", data
        finally:
            conn.close()


def reopen_approved_request(user_id: int, username: str = "",
                              full_name: str = "",
                              base: Optional[str] = None) -> bool:
    """Move an ``approved`` request back to ``pending`` for a retry.

    Used when the approval decision won its single-winner race but the
    follow-up allowed-list write failed (so the user is still
    unauthorized), and when /start finds an approved record whose user is
    no longer on the allowed list (manual remove raced the decision).
    Only transitions from ``approved`` -- rejected rows stay rejected --
    and clears ``decided_by`` so a later tap can win cleanly. Returns
    True when a row was re-armed.
    """
    now = time.time()
    with _lock:
        conn = _connect(base)
        try:
            cur = conn.execute(
                "UPDATE access_requests SET status=?, username=?, "
                "full_name=?, updated_at=?, decided_by=NULL "
                "WHERE user_id=? AND status=?",
                (STATUS_PENDING, username or "", full_name or "", now,
                 user_id, STATUS_APPROVED))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


def expire_request_on_manual_remove(user_id: int,
                                    base: Optional[str] = None) -> bool:
    """Expire an ``approved`` request after a manual allowed-list remove.

    Without this, a removed user keeps an ``approved`` record and their
    next /start would welcome them without notifying any admin. An
    expired record makes the next /start create a fresh request (new
    admin notifications), while rejected/pending history is untouched.
    Returns True when a row was expired.
    """
    with _lock:
        conn = _connect(base)
        try:
            cur = conn.execute(
                "UPDATE access_requests SET status=?, updated_at=? "
                "WHERE user_id=? AND status=?",
                (STATUS_EXPIRED, time.time(), user_id, STATUS_APPROVED))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


def record_admin_message(user_id: int, chat_id: int, message_id: int,
                         base: Optional[str] = None) -> None:
    """Remember an admin notification so its buttons can be retired later."""
    with _lock:
        conn = _connect(base)
        try:
            row = conn.execute(
                "SELECT admin_messages FROM access_requests WHERE user_id=?",
                (user_id,)).fetchone()
            if row is None:
                return
            try:
                messages = json.loads(row["admin_messages"] or "[]")
            except ValueError:
                messages = []
            entry = {"chat_id": chat_id, "message_id": message_id}
            if entry not in messages:
                messages.append(entry)
            conn.execute(
                "UPDATE access_requests SET admin_messages=? WHERE user_id=?",
                (json.dumps(messages), user_id))
            conn.commit()
        finally:
            conn.close()


def list_admin_messages(user_id: int, base: Optional[str] = None
                        ) -> List[Dict[str, int]]:
    data = get_request(user_id, base)
    if not data:
        return []
    messages = data.get("admin_messages") or []
    return [m for m in messages
            if isinstance(m, dict) and "chat_id" in m and "message_id" in m]


def format_full_name(first_name: str, last_name: str = "") -> str:
    return f"{first_name or ''} {last_name or ''}".strip()


def build_access_request_text(username: str, full_name: str,
                              user_id: int) -> str:
    """Admin DM text; untrusted parts are HTML-escaped before embedding."""
    lines = ["🔔 <b>New access request</b>", ""]
    if username:
        lines.append(f"👤 Username: @{html.escape(username)}")
    lines.append(f"📛 Name: {html.escape(full_name) if full_name else '—'}")
    lines.append(f"🆔 ID: <code>{int(user_id)}</code>")
    lines.append("")
    lines.append("Approve or reject with the buttons below.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Async wrappers (run the tiny blocking sqlite ops off the event loop)
# ---------------------------------------------------------------------------

async def arequest_access(user_id: int, username: str, full_name: str,
                          base: Optional[str] = None) -> str:
    return await asyncio.to_thread(request_access, user_id, username,
                                   full_name, base)


async def aget_request(user_id: int, base: Optional[str] = None
                       ) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(get_request, user_id, base)


async def aresolve_request(user_id: int, decision: str, decided_by: int,
                           base: Optional[str] = None
                           ) -> Tuple[str, Optional[Dict[str, Any]]]:
    return await asyncio.to_thread(resolve_request, user_id, decision,
                                   decided_by, base)


async def arecord_admin_message(user_id: int, chat_id: int, message_id: int,
                                 base: Optional[str] = None) -> None:
    await asyncio.to_thread(record_admin_message, user_id, chat_id,
                            message_id, base)


async def areopen_approved_request(user_id: int, username: str = "",
                                   full_name: str = "",
                                   base: Optional[str] = None) -> bool:
    return await asyncio.to_thread(reopen_approved_request, user_id,
                                   username, full_name, base)


async def aexpire_request_on_manual_remove(
        user_id: int, base: Optional[str] = None) -> bool:
    return await asyncio.to_thread(expire_request_on_manual_remove, user_id,
                                   base)


async def alist_admin_messages(user_id: int, base: Optional[str] = None
                               ) -> List[Dict[str, int]]:
    return await asyncio.to_thread(list_admin_messages, user_id, base)
