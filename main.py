import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, ErrorEvent
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from config import TELEGRAM_TOKEN, LOG_CHANNEL_ID
from filedb import load_allowed_users_cache
from states import UserState
import upload_queue
from handlers import (
    start_command,
    help_command,
    handle_file,
    handle_quiz_message,
    finish_extraction_callback,
    cancel_extraction_callback,
    show_questions_callback,
    cancel_processing_callback,
    confirm_send_callback,
    cancel_send_callback,
    collect_finish_callback,
    collect_cancel_callback,
    handle_text_message
)
from handlers_admin import (
    listusers_command,
    myaccess_command,
    userlist_command,
    AccessControlMiddleware,
    handle_allow_user_callback,
    handle_remove_user_callback,
    handle_admin_cancel_callback,
    handle_approve_callback,
    handle_reject_callback,
)

logger = logging.getLogger(__name__)

async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    os.makedirs("temp", exist_ok=True)
    
    bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.message.middleware(AccessControlMiddleware())
    dp.callback_query.middleware(AccessControlMiddleware())

    dp.message.register(start_command, CommandStart())
    dp.message.register(help_command, Command("help"))
    dp.message.register(myaccess_command, Command("myaccess"))
    
    dp.message.register(listusers_command, Command("listusers"))
    dp.message.register(userlist_command, Command("userlist"))

    dp.message.register(handle_file, F.document)
    dp.message.register(handle_quiz_message, F.poll.type == 'quiz')
    dp.message.register(handle_text_message, F.text & ~F.text.startswith('/'))

    dp.callback_query.register(finish_extraction_callback, F.data == "finish_extraction")
    dp.callback_query.register(cancel_extraction_callback, F.data == "cancel_extraction")
    dp.callback_query.register(show_questions_callback, F.data == "show_questions")
    dp.callback_query.register(cancel_processing_callback, F.data == "cancel_processing")
    dp.callback_query.register(confirm_send_callback, F.data.startswith("confirm_send:"))
    dp.callback_query.register(cancel_send_callback, F.data.startswith("cancel_send:"))
    dp.callback_query.register(collect_finish_callback, F.data == "collect_finish")
    dp.callback_query.register(collect_cancel_callback, F.data == "collect_cancel")

    dp.callback_query.register(handle_allow_user_callback, F.data.startswith("allow:"))
    dp.callback_query.register(handle_remove_user_callback, F.data.startswith("remove:"))
    dp.callback_query.register(handle_admin_cancel_callback, F.data == "admin_cancel")
    dp.callback_query.register(handle_approve_callback, F.data.startswith("approve:"))
    dp.callback_query.register(handle_reject_callback, F.data.startswith("reject:"))

    @dp.error()
    async def error_handler(event: ErrorEvent):
        logger.error(f"Update: {event.update}\nException: {event.exception}", exc_info=True)
        if LOG_CHANNEL_ID:
            try:
                await bot.send_message(LOG_CHANNEL_ID, f"❌ An error occurred: {event.exception}")
            except Exception as e:
                logger.error(f"Failed to send error to log channel: {e}")

    async def set_commands(bot_instance: Bot):
        commands = [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="help", description="Show help"),
            BotCommand(command="myaccess", description="Check your access")
        ]
        await bot_instance.set_my_commands(commands)

    await bot.delete_webhook(drop_pending_updates=True)
    load_allowed_users_cache()
    await set_commands(bot)

    # Global upload queue: two FIFO worker slots for ALL document uploads.
    # Workers need per-user FSM access for the preview step, hence the
    # factory; question records themselves stay on disk, never in FSM.
    def _fsm_factory(chat_id: int, user_id: int) -> FSMContext:
        return FSMContext(
            storage=dp.storage,
            key=StorageKey(chat_id=chat_id, user_id=user_id, bot_id=bot.id),
        )

    upload_workers = upload_queue.start_workers(
        bot, fsm_factory=_fsm_factory,
        awaiting_state=UserState.AWAITING_CONFIRMATION)
    try:
        requeues, cleaned = await asyncio.to_thread(upload_queue.recover_spool)
        revived = await asyncio.to_thread(upload_queue.reregister_previews)
        for manifest in requeues:
            upload_queue.requeue_job(manifest["job_id"])
        logger.info(
            f"Upload recovery: {len(requeues)} queued job(s) resumed, "
            f"{revived} live preview(s) re-registered, {cleaned} stale "
            f"spool entr(y/ies) cleaned.")
    except Exception as e:
        logger.error(f"Upload spool recovery failed: {e}", exc_info=True)
    
    logger.info("Bot is starting...")
    if LOG_CHANNEL_ID:
        await bot.send_message(LOG_CHANNEL_ID, "🚀 Bot has started successfully!")
        
    try:
        await dp.start_polling(bot)
    finally:
        for task in upload_workers:
            task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Bot stopped manually.")

