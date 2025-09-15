import logging
import os
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
    save_questions_to_file,
    get_temp_file_path
)
from filedb import upsert_user
from keyboards import get_main_keyboard, get_admin_keyboard
from handlers_admin import handle_admin_text_message
from states import UserState

logger = logging.getLogger(__name__)

AI_FORMATTING_PROMPT = """
ROLE: You are an AI assistant that flawlessly reformats quiz questions for a custom Telegram bot. Your primary goal is to ensure the output text is 100% parsable by the bot, with no extra text or conversation.

TASK: I will provide you with a set of questions and answers. You must reformat them according to the following strict rules and provide the result in a single, copyable block of text.

CRITICAL FORMATTING RULES:
1. Question Numbering: Each question must start on a new line with a number followed by a period (e.g., `1.`, `2.`, `3.`).
2. Options: Each multiple-choice option must be on its own new line, starting with a lowercase letter followed by a parenthesis (e.g., `a)`, `b)`, `c)`).
3. Answer Line: After all options, there must be a separate line that begins *exactly* with `Answer:` (case-sensitive, with a colon), followed by a space and the correct lowercase letter. Example: `Answer: c`.
4. Spacing: There must be exactly one blank line between each complete question block.
5. Output: The final output must be only the formatted questions. Do not include any introductory or concluding remarks.

PERFECT EXAMPLE:
```
1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c

2. Which planet is known as the Red Planet?
a) Jupiter
b) Mars
c) Venus
Answer: b
```

Now, please reformat the following questions into that exact structure:

[PASTE YOUR UNFORMATTED QUESTIONS HERE]
"""
BATCH_DELAY_SECONDS = 2.0

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
        "📚 **Help & Formatting Guide**\n\n"
        "**1. To Create Quizzes from a File:**\n"
        "Press '📝 Create Quiz' and send a PDF or `.txt` file with the correct format.\n\n"
        "**2. To Extract Forwarded Quizzes:**\n"
        "Press '📥 Extract Quizzes', forward your quizzes, then press '✅ Finish & Extract'.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

async def get_ai_prompt_command(message: types.Message):
    intro_text = "Copy this entire block and paste it into an AI, followed by your questions:"
    await message.answer(intro_text)
    await message.answer(f"<pre>{AI_FORMATTING_PROMPT}</pre>", parse_mode="HTML")

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

async def handle_file(message: types.Message, state: FSMContext):
    processing_msg = await message.reply("🔄 Processing file...")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(message.document.file_name)[1]) as temp_file:
            temp_path = temp_file.name
        await message.bot.download(message.document, destination=temp_path)
        text = await extract_text_from_file(temp_path)
        await process_quiz_extraction(message, state, text)
    finally:
        if temp_path: os.remove(temp_path)
        await processing_msg.delete()

async def process_quiz_extraction(message: types.Message, state: FSMContext, text: str):
    questions, skipped = extract_questions_from_text(text)
    if not questions:
        await message.reply("❌ No valid questions could be extracted.")
        await state.set_state(UserState.IDLE)
        return
    await state.update_data(extracted_questions=questions, skipped_questions=skipped)
    sent, failed, _, _ = await send_telegram_quizzes(message.bot, questions, message.chat.id, 1)
    result_msg = f"✅ Success! Sent {sent} quizzes."
    if skipped: result_msg += f"\n⚠️ Skipped {len(skipped)} items."
    if failed > 0: result_msg += f"\n❌ Failed to send {failed} quizzes."
    await message.reply(result_msg, reply_markup=get_file_processing_keyboard())

async def handle_text_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    current_state = await state.get_state()
    text = message.text

    if text == "📝 Create Quiz":
        await state.set_state(UserState.WAITING_FOR_FILE)
        await message.answer("Please send a file or paste the questions as text.")
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
    formatted = [f"{i}. {q['question']}\n" + "".join([f"{chr(97+j)}) {opt}\n" for j, opt in enumerate(q['options'])]) + f"Answer: {chr(97+q['correct_option_id'])}" for i, q in enumerate(questions, 1)]
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

