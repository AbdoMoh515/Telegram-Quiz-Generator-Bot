import logging
import os
import secrets
import asyncio
from datetime import datetime
from aiogram import types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from config import ADMIN_IDS
from utils import (
    extract_questions_from_text,
    send_telegram_quizzes,
    iter_disk_questions,
    stream_questions_to_export_file,
    format_quiz_as_text,
    format_question_export,
    build_preview_text,
    build_empty_result_text,
    is_allowed_document,
    save_questions_to_file,
    get_temp_file_path,
    MAX_INPUT_CHARS,
    MAX_COLLECT_MESSAGES,
    MAX_COLLECT_CHARS,
)
from filedb import upsert_user, is_user_allowed
from keyboards import (
    get_main_keyboard,
    get_admin_keyboard,
    get_preview_keyboard,
    get_collect_keyboard,
    get_access_request_keyboard,
)
from handlers_admin import handle_admin_text_message
from states import UserState
import upload_queue
from access_requests import (
    arequest_access,
    arecord_admin_message,
    areopen_approved_request,
    build_access_request_text,
    format_full_name,
)

logger = logging.getLogger(__name__)

AI_FORMATTING_PROMPT = """ROLE: You are an AI assistant that flawlessly reformats quiz questions for a custom Telegram bot. Your primary goal is to ensure the output text is 100% parsable by the bot, with no extra text or conversation.

TASK: I will provide you with a set of questions and answers in my next message. You must reformat them according to the strict rules below.

STRICT FORMAT 1 - MULTIPLE CHOICE (questions that have options):
1. Each question starts on a new line with a number followed by a period (e.g. 1.).
2. Each option is on its own new line, starting with a lowercase letter followed by a parenthesis (e.g. a), b), c)).
3. After all options, a separate line begins exactly with Answer: followed by a space and the correct lowercase letter only (e.g. Answer: c).
4. Optionally, a separate line AFTER the Answer line may carry a user-supplied clarification, written with the canonical label (e.g. Clarification: Because ...). Only include it when the user actually provided one.

STRICT FORMAT 2 - WRITTEN (questions with NO options):
1. Each question starts on a new line with a number followed by a period (e.g. 2.).
2. There are NO option lines at all.
3. The next line begins exactly with Answer: followed by a space and the full correct answer text (e.g. Answer: George Orwell).
4. Optionally, a separate line AFTER the Answer line may carry a user-supplied clarification, written with the canonical label (e.g. Clarification: Because ...). Only include it when the user actually provided one.

OPTIONAL CLARIFICATION:
1. A clarification is strictly optional and must NEVER be invented or generated: only carry through a clarification the user actually provided, whether it was labeled (Clarification: ... with the English label case-insensitive and the colon optional, or التوضيح: ...) or unlabeled free text after the Answer line.
2. When the user provided one, place it on its own separate line after the Answer line using the canonical label Clarification: followed by the user's text. When the user provided none, output nothing after the Answer line.
3. Example with a user-supplied clarification:
1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c
Clarification: Cairo has been the capital since the Fatimid era.

CRITICAL RULES:
1. Every question must be numbered and must include its own Answer: line (an optional clarification line may follow it).
2. Never write a single-letter answer without options, and never write full-text answers for questions that have options.
3. There must be exactly one blank line between each complete question block.
4. Output ONLY one fenced code block containing the formatted questions. No introductory or concluding remarks, no second code block, no prose outside the code block.

EXAMPLE OF YOUR ENTIRE REPLY:
```
1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c

2. Who wrote the novel 1984?
Answer: George Orwell
```

I will now paste my unformatted questions in the next message."""
BATCH_DELAY_SECONDS = 2.0

# Serializes preview claim (check-and-consume) across concurrent callbacks so
# double taps on ✅ Send / ❌ Cancel cannot dispatch or clear twice. The lock
# is held only for the quick state check-and-claim; network dispatch happens
# outside it.
_preview_claim_lock = asyncio.Lock()

CREATE_QUIZ_TEXT = (
    "Please send a <code>.txt</code> or <code>.md</code> file, "
    "or paste the questions as text.\n\n"
    "Supported formats:\n"
    "MCQ:\n"
    "<pre>1. Question?\na) Option 1\nb) Option 2\nAnswer: b</pre>\n"
    "Written (no options):\n"
    "<pre>1. Question?\nAnswer: Full answer text</pre>\n"
    "Optional clarification after the Answer line (only when you have one):\n"
    "<pre>Clarification: extra context</pre>\n"
    "(also accepts <code>التوضيح:</code> or unlabeled text on the line(s) "
    "after Answer; never invented by the bot).\n"
    "You will see a preview to confirm before anything is sent."
)

COLLECT_INTRO_TEXT = (
    "Collect mode started. Send your questions as separate messages "
    "(one or several per message is fine).\n\n"
    "Use the same strict format: numbered questions, options as "
    "<code>a)</code> lines for MCQ or no options for written, each ending "
    "with an <code>Answer:</code> line. You may add an optional "
    "<code>Clarification:</code> line (or <code>التوضيح:</code> / unlabeled "
    "text) after Answer only when you have extra context.\n\n"
    f"Limits: up to {MAX_COLLECT_MESSAGES} messages / "
    f"{MAX_COLLECT_CHARS} characters. Press ✅ Finish when done."
)


def get_quiz_collection_keyboard():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Finish & Extract", callback_data="finish_extraction")],
        [types.InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_extraction")]
    ])

def get_file_processing_keyboard():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📋 Show as Text", callback_data="show_questions")],
        [types.InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_processing")]
    ])

async def start_command(message: types.Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    username = getattr(user, "username", "") or ""
    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    # Capture the newest username/full name; existing rows keep their other
    # fields (date_joined, extras) for backward compatibility.
    upsert_user(user.id, username, first_name, last_name)
    if user.id in ADMIN_IDS or is_user_allowed(user.id):
        # Admins and already-approved users never trigger approval requests.
        await message.answer(
            "👋 Welcome to the Quiz Bot!", reply_markup=get_main_keyboard(user.id)
        )
        return
    full_name = format_full_name(first_name, last_name)
    disposition = await arequest_access(user.id, username, full_name)
    if disposition == "approved":
        # Never welcome on the request record alone: the allowed list is
        # the source of truth (a manual remove after approval leaves an
        # "approved" row behind). Re-arm such rows to pending so this
        # /start notifies admins again like a fresh request.
        if user.id in ADMIN_IDS or is_user_allowed(user.id):
            await message.answer(
                "👋 Welcome to the Quiz Bot!", reply_markup=get_main_keyboard(user.id)
            )
            return
        try:
            rearmed = await areopen_approved_request(
                user.id, username, full_name)
        except Exception as e:
            logger.warning(f"Could not re-open stale approved request for "
                           f"{user.id}: {e}")
            rearmed = False
        if rearmed:
            disposition = "created"
        else:
            # Lost a race (the row changed under us): fall back to a
            # pending notice rather than welcoming an unauthorized user.
            await message.reply(
                "⏳ Your access request is still pending. "
                "An administrator will review it soon."
            )
            return
    if disposition == "rejected":
        await message.reply(
            "❌ Your access request was declined. "
            "Contact an administrator if you believe this is a mistake."
        )
        return
    if disposition == "pending":
        # Repeat /start while a request is live: remind, never re-notify
        # admins (anti-spam).
        await message.reply(
            "⏳ Your access request is still pending. "
            "An administrator will review it soon."
        )
        return
    # Newly created request: DM every admin an inline Approve/Reject card.
    text = build_access_request_text(username, full_name, user.id)
    bot = getattr(message, "bot", None)
    if bot is not None and ADMIN_IDS:
        for admin_id in ADMIN_IDS:
            try:
                sent = await bot.send_message(
                    admin_id, text,
                    reply_markup=get_access_request_keyboard(user.id),
                )
                await arecord_admin_message(
                    user.id, admin_id, getattr(sent, "message_id", 0))
            except Exception as e:
                logger.warning(f"Could not notify admin {admin_id} of "
                               f"access request from {user.id}: {e}")
    await message.reply(
        "⏳ Your access request has been sent to the administrators. "
        "You will be notified once it is reviewed."
    )

async def help_command(message: types.Message):
    await message.answer(
        "📚 <b>Help &amp; Formatting Guide</b>\n\n"
        "<b>1. To Create Quizzes:</b>\n"
        "Press '📝 Create Quiz' and send a `.txt`/`.md` file or paste the "
        "questions as text. Both multiple-choice and written questions are "
        "supported:\n"
        "MCQ:\n"
        "<pre>1. What is the capital of Egypt?\n"
        "a) Giza\n"
        "b) Alexandria\n"
        "c) Cairo\n"
        "Answer: c</pre>\n"
        "Written (no options):\n"
        "<pre>2. Who wrote the novel 1984?\n"
        "Answer: George Orwell</pre>\n"
        "Optional clarification (only when you have one): add a separate line "
        "after Answer, e.g. <code>Clarification: extra context</code> "
        "(also accepts <code>التوضيح:</code> or unlabeled text).\n"
        "You will get a preview with Send/Cancel before anything is sent.\n\n"
        "<b>2. Collect Mode:</b>\n"
        "Press '🧩 Collect Messages' to send questions over several messages, "
        "then press '✅ Finish' for the same preview step.\n\n"
        "<b>3. To Extract Forwarded Quizzes:</b>\n"
        "Press '📥 Extract Quizzes', forward your quizzes, then press "
        "'✅ Finish &amp; Extract'.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

async def get_ai_prompt_command(message: types.Message):
    intro_text = "Copy this entire block and paste it into an AI, then send your questions as the next message. The AI will reply with a single copy-ready code block:"
    await message.answer(intro_text)
    # Copy-friendly chunked delivery: <pre> keeps the prompt verbatim and
    # Telegram caps a message at 4096 chars, so split on line boundaries.
    chunk = ""
    for line in AI_FORMATTING_PROMPT.splitlines(keepends=True):
        if len(chunk) + len(line) + len("<pre></pre>") > 4000:
            await message.answer(f"<pre>{chunk}</pre>", parse_mode="HTML")
            chunk = ""
        chunk += line
    if chunk:
        await message.answer(f"<pre>{chunk}</pre>", parse_mode="HTML")

async def process_quiz_batch(chat_id: int, state: FSMContext, bot: Bot):
    data = await state.get_data()
    quiz_buffer = data.get('quiz_buffer', [])
    if not quiz_buffer: return
    await state.update_data(quiz_buffer=[])
    all_quizzes = data.get('quizzes', [])
    all_quizzes.extend(quiz_buffer)
    await state.update_data(quizzes=all_quizzes)
    logger.info(f"Processed a batch of {len(quiz_buffer)}. Total: {len(all_quizzes)}.")
    last_reply_id = data.get('last_reply_message_id')
    text = f"✅ Batch of {len(quiz_buffer)} collected. Total: **{len(all_quizzes)}**."
    try:
        if last_reply_id:
            await bot.edit_message_text(text, chat_id, last_reply_id, reply_markup=get_quiz_collection_keyboard())
        else: raise TelegramBadRequest(method="editMessageText", message="No message to edit")
    except TelegramBadRequest:
        sent = await bot.send_message(chat_id, text, reply_markup=get_quiz_collection_keyboard())
        await state.update_data(last_reply_message_id=sent.message_id)

async def _delayed_batch_processor(delay: float, chat_id: int, state: FSMContext, bot: Bot):
    await asyncio.sleep(delay)
    await process_quiz_batch(chat_id, state, bot)

async def handle_quiz_message(message: types.Message, state: FSMContext, bot: Bot):
    current_state = await state.get_state()
    if current_state != UserState.COLLECTING_QUIZZES: return
    data = await state.get_data()
    quiz_buffer = data.get('quiz_buffer', [])
    quiz_buffer.append(message.poll)
    batch_task = data.get('batch_task')
    if batch_task: batch_task.cancel()
    new_task = asyncio.create_task(
        _delayed_batch_processor(BATCH_DELAY_SECONDS, message.chat.id, state, bot)
    )
    await state.update_data(quiz_buffer=quiz_buffer, batch_task=new_task)

# ---------------------------------------------------------------------------
# Parsed-question path (pasted text / .txt/.md files / collect mode)
# ---------------------------------------------------------------------------

def _clear_preview_data_kwargs():
    return {
        'preview_questions': None,
        'preview_skipped': None,
        'preview_token': None,
        'preview_consumed': True,
    }


async def process_quiz_extraction(message: types.Message, state: FSMContext, text: str,
                              actor_id: int | None = None):
    """Parse *text* and show the preview/confirmation step (never sends yet).

    *actor_id* overrides the keyboard user when the caller is a callback
    query (whose ``message.from_user`` is the bot, not the tapping user).
    """
    user_id = actor_id if actor_id is not None else (
        message.from_user.id if message.from_user else None
    )
    if len(text or "") > MAX_INPUT_CHARS:
        await message.reply(
            f"❌ Text is too long ({len(text)} chars, max {MAX_INPUT_CHARS}). "
            "Please split it into smaller parts.",
            reply_markup=get_main_keyboard(user_id) if user_id else None,
        )
        await state.set_state(UserState.IDLE)
        return
    questions, skipped = extract_questions_from_text(text or "")
    if not questions:
        await message.reply(
            build_empty_result_text(skipped),
            reply_markup=get_main_keyboard(user_id) if user_id else None,
        )
        await state.update_data(**_clear_preview_data_kwargs())
        await state.set_state(UserState.IDLE)
        return
    token = secrets.token_hex(8)
    await state.update_data(
        preview_questions=questions,
        preview_skipped=skipped,
        preview_token=token,
        preview_consumed=False,
        extracted_questions=questions,
        skipped_questions=skipped,
    )
    await state.set_state(UserState.AWAITING_CONFIRMATION)
    await message.reply(
        build_preview_text(questions, skipped),
        reply_markup=get_preview_keyboard(token),
    )


async def _check_upload_token_owner(callback_query: types.CallbackQuery,
                                    token: str):
    """Refuse foreign taps on a disk-backed upload preview.

    Returns "ok" when the tapping user/chat owns the preview, "forbidden"
    (after answering) when they do not, and None when the token is not a
    live upload preview (stale/legacy -- the caller falls through to the
    existing dead-token/legacy handling). Runs BEFORE any consume, so
    unauthorized presses can neither dispatch nor cancel anything.
    """
    info = await upload_queue.describe_upload_token(token)
    if info is None:
        return None
    actor_id = getattr(callback_query.from_user, "id", None)
    if actor_id is not None and info.get("user_id") != actor_id:
        await callback_query.answer(
            "This preview belongs to another user.", show_alert=True)
        return "forbidden"
    message = getattr(callback_query, "message", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and info.get("chat_id") != chat_id:
        await callback_query.answer(
            "This preview belongs to another chat.", show_alert=True)
        return "forbidden"
    return "ok"


async def _check_preview_token(callback_query: types.CallbackQuery, state: FSMContext):
    """Validate the preview callback against the stored token.

    Returns the state data dict if valid, otherwise None (after answering
    the callback query with an appropriate notice).
    """
    data = await state.get_data()
    current_state = await state.get_state()
    parts = (callback_query.data or "").split(":", 1)
    cb_token = parts[1] if len(parts) == 2 else None
    stored_token = data.get("preview_token")
    if current_state != UserState.AWAITING_CONFIRMATION or not stored_token:
        await callback_query.answer(
            "Nothing to confirm. Please submit your questions again.", show_alert=True
        )
        return None
    if not cb_token or cb_token != stored_token:
        await callback_query.answer(
            "This preview is stale. Please use the latest one.", show_alert=True
        )
        return None
    if data.get("preview_consumed"):
        await callback_query.answer("Already processed.", show_alert=True)
        return None
    return data


async def confirm_send_callback(callback_query: types.CallbackQuery, state: FSMContext, bot: Bot):
    parts = (callback_query.data or "").split(":", 1)
    if len(parts) == 2:
        # Disk-backed upload preview (token registry, not FSM lists).
        # Ownership is verified before the claim so a foreign tap can
        # neither dispatch nor consume the token. Only the quick
        # check-and-claim runs under the lock; dispatch (0.5s per
        # question) happens outside it so one large upload never blocks
        # other users' Send/Cancel.
        ownership = await _check_upload_token_owner(
            callback_query, parts[1])
        if ownership == "forbidden":
            return
        job = None
        dead = None
        async with _preview_claim_lock:
            job = await upload_queue.consume_upload_token(parts[1])
            if job is None:
                dead = upload_queue.dead_token_reason(parts[1])
        if job is not None:
            await _confirm_upload_send(callback_query, state, bot, job)
            return
        if dead == upload_queue.DEAD_CONSUMED:
            await callback_query.answer("Already processed.",
                                        show_alert=True)
            return
        if dead == upload_queue.DEAD_EXPIRED:
            await callback_query.answer(
                "That result has expired. "
                "Please upload the file again.", show_alert=True)
            return
        # Unknown token: a legacy in-memory preview (handled below) or
        # truly stale (the legacy check answers accordingly).
    async with _preview_claim_lock:
        data = await _check_preview_token(callback_query, state)
        if data is None:
            return
        # Claim the preview first so double-taps cannot dispatch twice.
        await state.update_data(preview_consumed=True)
        questions = list(data.get("preview_questions") or [])
        skipped = list(data.get("preview_skipped") or [])
    await callback_query.answer("Sending...")
    if not questions:
        try:
            await callback_query.message.edit_text("❌ Nothing valid to send.")
        except TelegramBadRequest:
            pass
        await state.update_data(**_clear_preview_data_kwargs())
        await state.set_state(UserState.IDLE)
        return
    try:
        sent, failed, _, _ = await send_telegram_quizzes(
            bot, questions, callback_query.message.chat.id, 1
        )
    except Exception as e:
        logger.error(f"Dispatch failed: {e}", exc_info=True)
        await state.update_data(**_clear_preview_data_kwargs())
        await state.set_state(UserState.IDLE)
        try:
            await callback_query.message.edit_text("❌ Dispatch failed. Nothing was sent.")
        except TelegramBadRequest:
            pass
        return
    result_msg = f"✅ Success! Sent {sent} question(s)."
    if skipped:
        result_msg += f"\n⚠️ Skipped {len(skipped)} item(s)."
    if failed > 0:
        result_msg += f"\n❌ Failed to send {failed} question(s)."
    try:
        await callback_query.message.edit_text("📤 Dispatched. See summary below.")
    except TelegramBadRequest:
        pass
    # Keep extracted_questions for the "Show as Text" export, but the preview
    # token is consumed so this callback cannot re-send.
    await state.update_data(**_clear_preview_data_kwargs())
    await state.set_state(UserState.IDLE)
    await callback_query.message.answer(
        result_msg, reply_markup=get_file_processing_keyboard()
    )


async def cancel_send_callback(callback_query: types.CallbackQuery, state: FSMContext):
    parts = (callback_query.data or "").split(":", 1)
    if len(parts) == 2:
        # Disk-backed upload preview (token registry, not FSM lists).
        # Ownership is verified before the claim so a foreign tap can
        # neither cancel nor consume the token. Only the quick claim
        # runs under the lock; disk/network cleanup happens outside it
        # so a slow cancel never blocks other users.
        ownership = await _check_upload_token_owner(
            callback_query, parts[1])
        if ownership == "forbidden":
            return
        job = None
        dead = None
        async with _preview_claim_lock:
            job = await upload_queue.consume_upload_token(parts[1])
            if job is None:
                dead = upload_queue.dead_token_reason(parts[1])
        if job is not None:
            await _cancel_upload_send(callback_query, state, job)
            return
        if dead == upload_queue.DEAD_CONSUMED:
            await callback_query.answer("Already processed.",
                                        show_alert=True)
            return
        if dead == upload_queue.DEAD_EXPIRED:
            await callback_query.answer(
                "That result has expired. "
                "Please upload the file again.", show_alert=True)
            return
        # Unknown token: a legacy in-memory preview (handled below) or
        # truly stale (the legacy check answers accordingly).
    async with _preview_claim_lock:
        data = await _check_preview_token(callback_query, state)
        if data is None:
            return
        await state.update_data(**_clear_preview_data_kwargs())
        await state.set_state(UserState.IDLE)
    await callback_query.answer("Cancelled.", show_alert=True)
    try:
        await callback_query.message.edit_text("❌ Send cancelled. Nothing was sent.")
    except TelegramBadRequest:
        pass
    await callback_query.message.answer(
        "Returning to the main menu.",
        reply_markup=get_main_keyboard(callback_query.from_user.id),
    )


async def _append_collect_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    buffer = data.get("collect_buffer") or []
    used_chars = sum(len(part) for part in buffer)
    text = message.text or ""
    if len(buffer) >= MAX_COLLECT_MESSAGES or used_chars + len(text) > MAX_COLLECT_CHARS:
        await message.reply(
            f"⚠️ Collect buffer is full ({len(buffer)} messages). "
            "Press ✅ Finish to preview, or ❌ Cancel to discard.",
            reply_markup=get_collect_keyboard(),
        )
        return
    buffer.append(text)
    await state.update_data(collect_buffer=buffer)
    await message.reply(
        f"📥 Collected {len(buffer)} message(s). Keep sending or press ✅ Finish.",
        reply_markup=get_collect_keyboard(),
    )


async def collect_finish_callback(callback_query: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != UserState.COLLECTING_TEXT:
        await callback_query.answer("Collect mode is not active.", show_alert=True)
        return
    data = await state.get_data()
    buffer = data.get("collect_buffer") or []
    if not any(part.strip() for part in buffer):
        await callback_query.answer("No messages collected yet.", show_alert=True)
        return
    await callback_query.answer("Processing...")
    await state.update_data(collect_buffer=[])
    combined = "\n\n".join(buffer)
    # process_quiz_extraction only needs .reply for output, but its keyboard
    # user must be the tapping user (callback_query.from_user), not
    # callback_query.message.from_user (the bot).
    actor_id = callback_query.from_user.id if callback_query.from_user else None
    await process_quiz_extraction(callback_query.message, state, combined,
                                  actor_id=actor_id)


async def collect_cancel_callback(callback_query: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != UserState.COLLECTING_TEXT:
        await callback_query.answer("Collect mode is not active.", show_alert=True)
        return
    await state.update_data(collect_buffer=[])
    await state.set_state(UserState.IDLE)
    await callback_query.answer("Cancelled.", show_alert=True)
    try:
        await callback_query.message.edit_text("❌ Collection cancelled. Nothing was sent.")
    except TelegramBadRequest:
        pass
    await callback_query.message.answer(
        "Returning to the main menu.",
        reply_markup=get_main_keyboard(callback_query.from_user.id),
    )


async def handle_file(message: types.Message, state: FSMContext):
    file_name = getattr(message.document, "file_name", None)
    if not is_allowed_document(file_name):
        await message.reply(
            "❌ Only <code>.txt</code> and <code>.md</code> files are supported. "
            "Please convert your file to plain text and try again."
        )
        return
    file_id = getattr(message.document, "file_id", None)
    if not file_id:
        await message.reply(
            "❌ Could not read that file reference. Please re-upload the file."
        )
        return
    # Never download here: every upload waits its turn in the single global
    # FIFO (at most two process at once). Only small metadata is queued, so
    # extra uploads stay fair instead of downloading concurrently. FSM
    # holds no per-upload list: each preview is tracked by its own small
    # job id / token pair written when the preview is delivered.
    job_id, position = await upload_queue.enqueue_upload(
        message.from_user.id, message.chat.id, file_id, file_name)
    if position <= 1:
        await message.reply(
            "🔄 Processing file... I'll send a preview here when it's ready."
        )
    else:
        await message.reply(
            f"📥 Queued at position #{position}. At most two files process "
            "at once; I'll send your preview here when it's your turn."
        )


def _clear_upload_preview_kwargs():
    return {
        'preview_job_id': None,
        'preview_valid_count': None,
        'preview_skipped_count': None,
    }


async def _confirm_upload_send(callback_query: types.CallbackQuery,
                               state: FSMContext, bot: Bot,
                               job: dict):
    """Dispatch a disk-backed upload preview claimed from the queue."""
    job_id = job["_job_id"]
    await callback_query.answer("Sending...")
    try:
        sent, failed, _, _ = await send_telegram_quizzes(
            bot, iter_disk_questions(upload_queue.questions_path(job_id)),
            callback_query.message.chat.id, 1,
        )
    except Exception as e:
        logger.error(f"Upload dispatch failed for job {job_id}: {e}",
                     exc_info=True)
        await upload_queue.mark_upload_cancelled(job_id)
        await state.update_data(**_clear_preview_data_kwargs(),
                                **_clear_upload_preview_kwargs())
        await state.set_state(UserState.IDLE)
        try:
            await callback_query.message.edit_text(
                "❌ Dispatch failed. Nothing was sent.")
        except TelegramBadRequest:
            pass
        return
    skipped_count = int(job.get("skipped_count") or 0)
    result_msg = f"✅ Success! Sent {sent} question(s)."
    if skipped_count:
        result_msg += f"\n⚠️ Skipped {skipped_count} item(s)."
    if failed > 0:
        result_msg += f"\n❌ Failed to send {failed} question(s)."
    try:
        await callback_query.message.edit_text("📤 Dispatched. See summary below.")
    except TelegramBadRequest:
        pass
    await upload_queue.mark_upload_dispatched(job_id)
    # Keep extracted_job_id for the "Show as Text" export, but the preview
    # token is consumed so this message cannot re-send.
    await state.update_data(**_clear_preview_data_kwargs(),
                            **_clear_upload_preview_kwargs(),
                            extracted_job_id=job_id)
    await state.set_state(UserState.IDLE)
    await callback_query.message.answer(
        result_msg, reply_markup=get_file_processing_keyboard()
    )


async def _cancel_upload_send(callback_query: types.CallbackQuery,
                              state: FSMContext, job: dict):
    """Discard a disk-backed upload preview claimed from the queue."""
    job_id = job["_job_id"]
    await upload_queue.mark_upload_cancelled(job_id)
    await state.update_data(**_clear_preview_data_kwargs(),
                            **_clear_upload_preview_kwargs(),
                            extracted_job_id=None)
    await state.set_state(UserState.IDLE)
    await callback_query.answer("Cancelled.", show_alert=True)
    try:
        await callback_query.message.edit_text("❌ Send cancelled. Nothing was sent.")
    except TelegramBadRequest:
        pass
    await callback_query.message.answer(
        "Returning to the main menu.",
        reply_markup=get_main_keyboard(callback_query.from_user.id),
    )

async def handle_text_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    current_state = await state.get_state()
    text = message.text

    if text == "📝 Create Quiz":
        await state.set_state(UserState.WAITING_FOR_FILE)
        await message.answer(CREATE_QUIZ_TEXT)
    elif text == "🧩 Collect Messages":
        await state.set_state(UserState.COLLECTING_TEXT)
        await state.update_data(collect_buffer=[])
        await message.answer(COLLECT_INTRO_TEXT, reply_markup=get_collect_keyboard())
    elif text == "📥 Extract Quizzes from Forwards":
        await state.set_state(UserState.COLLECTING_QUIZZES)
        await state.update_data(quizzes=[], quiz_buffer=[], batch_task=None, last_reply_message_id=None)
        await message.answer("I'm ready. Forward your quizzes now.")
    elif text == "❓ Help":
        await help_command(message)
    elif text == "🤖 Get AI Prompt":
        await get_ai_prompt_command(message)
    elif text == "👑 Admin Panel" and user_id in ADMIN_IDS:
        await state.set_state(UserState.ADMIN_PANEL)
        await message.answer("👑 Welcome to the Admin Panel!", reply_markup=get_admin_keyboard())
    elif current_state == UserState.ADMIN_PANEL:
        await handle_admin_text_message(message, state)
    elif current_state == UserState.WAITING_FOR_FILE:
        await process_quiz_extraction(message, state, text)
    elif current_state == UserState.COLLECTING_TEXT:
        await _append_collect_message(message, state)
    elif current_state == UserState.AWAITING_CONFIRMATION:
        await message.reply("Please confirm or cancel the preview above first.")
    else:
        await message.reply("Please use the keyboard buttons.", reply_markup=get_main_keyboard(user_id))

async def finish_extraction_callback(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quiz_buffer = data.get('quiz_buffer', [])
    all_quizzes = data.get('quizzes', [])
    all_quizzes.extend(quiz_buffer)
    if not all_quizzes:
        await callback_query.answer("You haven't forwarded any quizzes.", show_alert=True)
        return
    await callback_query.answer("Processing...")
    try:
        await callback_query.message.edit_text(f"🔄 Processing {len(all_quizzes)} quizzes...")
    except TelegramBadRequest: pass
    formatted_quizzes = [await format_quiz_as_text(q, i) for i, q in enumerate(all_quizzes, 1)]
    summary = f"✅ Extracted {len(formatted_quizzes)} quizzes."
    file_path = get_temp_file_path(callback_query.from_user.id)
    save_questions_to_file(formatted_quizzes, file_path)
    await callback_query.message.answer_document(
        types.FSInputFile(file_path, filename="extracted_quizzes.txt"), caption=summary
    )
    os.remove(file_path)
    try: await callback_query.message.delete()
    except TelegramBadRequest: pass
    await state.clear()

async def cancel_extraction_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback_query.answer("Cancelled.", show_alert=True)
    try: await callback_query.message.delete()
    except TelegramBadRequest: pass
    await callback_query.message.answer("❌ Extraction cancelled.", reply_markup=get_main_keyboard(callback_query.from_user.id))

async def show_questions_callback(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    job_id = data.get('extracted_job_id')
    if job_id:
        # Disk-backed upload result: stream the export in bounded memory.
        if not upload_queue.job_files_exist(job_id):
            await callback_query.answer(
                "That result has expired (cleaned up). "
                "Please upload the file again.", show_alert=True)
            await state.update_data(extracted_job_id=None)
            return
        await callback_query.answer("Generating text file...")
        export_name = f"upload_export_{job_id}.txt"
        export_dir = os.environ.get("BOT_TEMP_DIR", "temp")
        os.makedirs(export_dir, exist_ok=True)
        export_path = os.path.join(export_dir, export_name)
        try:
            count = await asyncio.to_thread(
                stream_questions_to_export_file,
                upload_queue.questions_path(job_id), export_path)
            await callback_query.message.answer_document(
                types.FSInputFile(export_path, filename="extracted_questions.txt"),
                caption=f"📋 Here are the {count} extracted questions."
            )
        finally:
            try:
                os.remove(export_path)
            except OSError:
                pass
        return
    questions = data.get('extracted_questions')
    if not questions:
        await callback_query.answer("No data found.", show_alert=True)
        return
    await callback_query.answer("Generating text file...")
    formatted = [format_question_export(q, i) for i, q in enumerate(questions, 1)]
    file_path = get_temp_file_path(callback_query.from_user.id, prefix="extracted_text_")
    save_questions_to_file(formatted, file_path)
    await callback_query.message.answer_document(
        types.FSInputFile(file_path, filename="extracted_questions.txt"),
        caption=f"📋 Here are the {len(questions)} extracted questions."
    )
    os.remove(file_path)

async def cancel_processing_callback(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    job_id = data.get('extracted_job_id')
    if job_id:
        # Drop the disk-backed upload result too (best effort).
        await upload_queue.mark_upload_cancelled(job_id)
    await state.clear()
    await callback_query.answer("Cancelled.", show_alert=True)
    await callback_query.message.edit_text("❌ Process cancelled.")
    await callback_query.message.answer("Returning to the main menu.", reply_markup=get_main_keyboard(callback_query.from_user.id))
