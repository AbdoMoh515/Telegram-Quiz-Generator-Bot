import logging
import os
import secrets
import tempfile
import asyncio
from datetime import datetime
from aiogram import types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from config import ADMIN_IDS
from utils import (
    extract_text_from_file,
    extract_questions_from_text,
    send_telegram_quizzes,
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
from filedb import upsert_user
from keyboards import (
    get_main_keyboard,
    get_admin_keyboard,
    get_preview_keyboard,
    get_collect_keyboard,
)
from handlers_admin import handle_admin_text_message
from states import UserState

logger = logging.getLogger(__name__)

AI_FORMATTING_PROMPT = """ROLE: You are an AI assistant that flawlessly reformats quiz questions for a custom Telegram bot. Your primary goal is to ensure the output text is 100% parsable by the bot, with no extra text or conversation.

TASK: I will provide you with a set of questions and answers in my next message. You must reformat them according to the strict rules below.

STRICT FORMAT 1 - MULTIPLE CHOICE (questions that have options):
1. Each question starts on a new line with a number followed by a period (e.g. 1.).
2. Each option is on its own new line, starting with a lowercase letter followed by a parenthesis (e.g. a), b), c)).
3. After all options, a separate line begins exactly with Answer: followed by a space and the correct lowercase letter only (e.g. Answer: c).

STRICT FORMAT 2 - WRITTEN (questions with NO options):
1. Each question starts on a new line with a number followed by a period (e.g. 2.).
2. There are NO option lines at all.
3. The next line begins exactly with Answer: followed by a space and the full correct answer text (e.g. Answer: George Orwell).

CRITICAL RULES:
1. Every question must be numbered and must end with its own Answer: line.
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
    "You will see a preview to confirm before anything is sent."
)

COLLECT_INTRO_TEXT = (
    "Collect mode started. Send your questions as separate messages "
    "(one or several per message is fine).\n\n"
    "Use the same strict format: numbered questions, options as "
    "<code>a)</code> lines for MCQ or no options for written, each ending "
    "with an <code>Answer:</code> line.\n\n"
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
    upsert_user(user.id, user.username, user.first_name)
    await message.answer(
        "👋 Welcome to the Quiz Bot!", reply_markup=get_main_keyboard(user.id)
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
    processing_msg = await message.reply("🔄 Processing file...")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=os.path.splitext(file_name)[1].lower()
        ) as temp_file:
            temp_path = temp_file.name
        await message.bot.download(message.document, destination=temp_path)
        text = await extract_text_from_file(temp_path)
        if not (text or "").strip():
            await message.reply("❌ Could not read any text from that file.")
            await state.set_state(UserState.IDLE)
            return
        await process_quiz_extraction(message, state, text)
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        try:
            await processing_msg.delete()
        except Exception:
            pass

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
    await state.clear()
    await callback_query.answer("Cancelled.", show_alert=True)
    await callback_query.message.edit_text("❌ Process cancelled.")
    await callback_query.message.answer("Returning to the main menu.", reply_markup=get_main_keyboard(callback_query.from_user.id))
