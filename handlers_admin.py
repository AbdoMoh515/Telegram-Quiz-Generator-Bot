import asyncio
import logging
from typing import Callable, Dict, Any, Awaitable, Optional
from aiogram import types, BaseMiddleware
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from filedb import is_user_allowed, add_allowed_user_from_user, get_user_by_id
from access_requests import (
    aresolve_request,
    alist_admin_messages,
    areopen_approved_request,
    build_access_request_text,
    STATUS_APPROVED,
    STATUS_REJECTED,
)
from config import ADMIN_IDS
from keyboards import get_main_keyboard, get_admin_keyboard
from states import UserState

logger = logging.getLogger(__name__)

# Per-request-user locks serializing concurrent Approve/Reject taps for
# the SAME target user. The DB compare-and-set already picks a single
# winner, but without this a second admin tapping DURING the winner's
# allowed-list write would resolve to "already", retire both admin cards
# (removing live buttons), and -- if the winner's write then failed --
# leave a re-opened pending request with no buttons. Holding the
# per-user lock from resolve through retire keeps buttons live until
# the outcome is final; a waiter either sees the final result (and
# re-retires idempotently) or wins the retry itself. One lock per
# target user: different users never block each other.
_decision_locks: Dict[int, asyncio.Lock] = {}


def _decision_lock_for(user_id: int) -> asyncio.Lock:
    lock = _decision_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _decision_locks[user_id] = lock
    return lock

class AccessControlMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get('event_from_user')
        if not user:
            return await handler(event, data)
        
        public_commands = ('/start', '/help', '/myaccess')
        if isinstance(event, Message) and event.text and event.text.startswith(public_commands):
            return await handler(event, data)
        
        if user.id in ADMIN_IDS or is_user_allowed(user.id):
            return await handler(event, data)
        
        if isinstance(event, Message):
            await event.reply("❌ **Access Denied**\nYou are not authorized to use this bot.")
        elif isinstance(event, CallbackQuery):
            await event.answer("❌ Access Denied", show_alert=True)
        
        logger.warning(f"Denied access for unauthorized user {user.id} (@{user.username})")
        return

async def myaccess_command(message: Message):
    user_id = message.from_user.id
    if user_id in ADMIN_IDS:
        await message.reply("👑 You are a **Bot Administrator**.")
    elif is_user_allowed(user_id):
        await message.reply("✅ You are an **Allowed User**.")
    else:
        await message.reply("❌ You are **not authorized**.")

async def handle_admin_text_message(message: Message, state: FSMContext):
    text = message.text
    if text == "⬅️ Back to Main Menu":
        await state.set_state(UserState.IDLE)
        await message.answer("⬅️ Returning to the main menu.", reply_markup=get_main_keyboard(message.from_user.id))

async def handle_admin_cancel_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    await callback_query.answer("Cancelled.")
    await state.set_state(UserState.ADMIN_PANEL)


# ---------------------------------------------------------------------------
# Access-request approvals (DM'd to admins on unapproved /start)
# ---------------------------------------------------------------------------

def _parse_request_target(callback_query: CallbackQuery) -> Optional[int]:
    try:
        return int((callback_query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return None


async def _retire_request_messages(bot, user_id: int, row: Optional[Dict],
                                   outcome_line: str) -> None:
    """Replace every admin notification's buttons with the final outcome.

    Best effort per message ("when feasible"): a DM the bot can no longer
    edit (deleted, blocked) is simply skipped.
    """
    messages = await alist_admin_messages(user_id)
    username = (row or {}).get("username", "")
    full_name = (row or {}).get("full_name", "")
    base_text = build_access_request_text(username, full_name, user_id)
    final_text = f"{base_text}\n\n{outcome_line}"
    for entry in messages:
        try:
            await bot.edit_message_text(
                final_text, entry["chat_id"], entry["message_id"],
                reply_markup=None)
        except Exception as e:
            logger.warning(f"Could not retire access-request message "
                           f"{entry}: {e}")


async def _handle_access_decision(callback_query: CallbackQuery, decision: str,
                                  bot=None) -> None:
    admin_id = callback_query.from_user.id
    if bot is None:
        bot = getattr(callback_query, "bot", None)
    if bot is None:
        await callback_query.answer("Bot unavailable.", show_alert=True)
        return
    if admin_id not in ADMIN_IDS:
        await callback_query.answer("❌ Admins only.", show_alert=True)
        return
    target_id = _parse_request_target(callback_query)
    if target_id is None:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    # Serialize taps for the same target user (see _decision_locks):
    # the loser waits instead of retiring live buttons mid-write.
    async with _decision_lock_for(target_id):
        await _handle_access_decision_locked(
            callback_query, decision, bot, admin_id, target_id)


async def _handle_access_decision_locked(callback_query: CallbackQuery,
                                         decision: str, bot,
                                         admin_id: int,
                                         target_id: int) -> None:
    outcome, row = await aresolve_request(target_id, decision, admin_id)
    row = row or {}
    if outcome == "missing":
        await callback_query.answer("That request no longer exists.",
                                    show_alert=True)
        try:
            await callback_query.message.edit_text(
                "⌛️ This access request is no longer pending.")
        except Exception:
            pass
        return
    if outcome == "already":
        previous = row.get("status", "resolved")
        decided_by = row.get("decided_by")
        note = (f"Already {previous}"
                + (f" by admin <code>{decided_by}</code>."
                   if decided_by else "."))
        await callback_query.answer("Already resolved.", show_alert=True)
        await _retire_request_messages(
            bot, target_id, row,
            f"✅ Approved." if previous == STATUS_APPROVED
            else f"❌ Rejected. ({note})" if previous == STATUS_REJECTED
            else note)
        return
    # This admin won the race.
    if decision == STATUS_APPROVED:
        user = get_user_by_id(target_id)
        if user is None:
            # users.json entry missing (e.g. reset after the request):
            # fall back to the request's own snapshot so approval still
            # takes effect instead of failing silently.
            user = {"id": target_id,
                    "username": row.get("username", ""),
                    "first_name": row.get("full_name", "")}
        try:
            # Off the event loop: a slow/failed disk write must not
            # stall other callbacks; a concurrent tap for this same
            # user waits on our per-user lock above (never retires
            # live buttons mid-write), while other users proceed.
            allowed_ok = await asyncio.to_thread(
                add_allowed_user_from_user, user)
        except Exception as e:
            logger.error(f"Allowed-list write failed for approved user "
                         f"{target_id}: {e}", exc_info=True)
            allowed_ok = False
        if not allowed_ok:
            # Transactional retry: the decision won the single-winner
            # race, but the user is still unauthorized, so send the
            # request back to pending instead of leaving an "approved"
            # row for an unauthorized user. Buttons stay live, nobody
            # is DM'd, and a later Approve tap can win cleanly -- at
            # most one tap ever sends the user DM (see success path).
            try:
                rearmed = await areopen_approved_request(
                    target_id, row.get("username", ""),
                    row.get("full_name", ""))
            except Exception as e:
                logger.error(f"Could not re-open access request for "
                             f"{target_id}: {e}", exc_info=True)
                rearmed = False
            if rearmed:
                await callback_query.answer(
                    "⚠️ Approved, but saving to the allowed list failed -- "
                    "the request was re-opened. Tap Approve again to retry.",
                    show_alert=True)
            else:
                await callback_query.answer(
                    "⚠️ Approved, but saving to the allowed list failed -- "
                    "please retry later or edit allowed_users.json on the "
                    "server, then restart the bot.",
                    show_alert=True)
            return
        outcome_line = (f"✅ Approved by admin <code>{admin_id}</code>.")
        try:
            await bot.send_message(
                target_id,
                "✅ Your access request was <b>approved</b>! "
                "Press /start to begin using the bot.")
        except Exception as e:
            logger.warning(f"Could not DM approval to {target_id}: {e}")
        await _retire_request_messages(bot, target_id, row, outcome_line)
        await callback_query.answer("User approved.")
    else:
        outcome_line = f"❌ Rejected by admin <code>{admin_id}</code>."
        try:
            await bot.send_message(
                target_id,
                "❌ Your access request was <b>declined</b>. "
                "Contact an administrator if you believe this is a mistake.")
        except Exception as e:
            logger.warning(f"Could not DM rejection to {target_id}: {e}")
        await _retire_request_messages(bot, target_id, row, outcome_line)
        await callback_query.answer("User rejected.")


async def handle_approve_callback(callback_query: CallbackQuery,
                                  bot=None) -> None:
    await _handle_access_decision(callback_query, STATUS_APPROVED, bot)


async def handle_reject_callback(callback_query: CallbackQuery,
                                 bot=None) -> None:
    await _handle_access_decision(callback_query, STATUS_REJECTED, bot)

