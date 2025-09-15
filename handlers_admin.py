import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import types, BaseMiddleware
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from filedb import is_user_allowed, add_allowed_user_from_user, list_allowed_users, list_all_users, get_user_by_id, remove_allowed_user
from config import ADMIN_IDS
from keyboards import get_main_keyboard, get_admin_keyboard, create_user_selection_keyboard
from states import UserState

logger = logging.getLogger(__name__)

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

async def listusers_command(message: Message):
    users = list_allowed_users()
    if not users:
        await message.reply("No users are currently on the allowed list.")
        return
    msg_parts = ["<b>📋 Allowed Users:</b>"]
    for u in users:
        msg_parts.append(f"  • <code>{u['id']}</code> - {u.get('first_name', 'N/A')} (@{u.get('username', 'N/A')})")
    await message.reply("\n".join(msg_parts))

async def userlist_command(message: Message):
    users = list_all_users()
    if not users:
        await message.reply("No users have started the bot yet.")
        return
    msg_parts = ["<b>👥 All Users in Database:</b>"]
    for u in users:
        msg_parts.append(f"  • <code>{u['id']}</code> - {u.get('first_name', 'N/A')} (@{u.get('username', 'N/A')})")
    await message.reply("\n".join(msg_parts))

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
    if text == "✅ Allow User":
        await state.set_state(UserState.CHOOSING_USER_TO_ALLOW)
        all_user_ids = {u['id'] for u in list_all_users()}
        allowed_user_ids = {u['id'] for u in list_allowed_users()}
        unallowed_ids = all_user_ids - allowed_user_ids
        unallowed_users = [get_user_by_id(uid) for uid in unallowed_ids if get_user_by_id(uid)]
        
        if not unallowed_users:
            await message.answer("All known users are already on the allowed list.", reply_markup=get_admin_keyboard())
            await state.set_state(UserState.ADMIN_PANEL)
            return
        
        keyboard = create_user_selection_keyboard(unallowed_users, "allow")
        await message.answer("Select a user to allow:", reply_markup=keyboard)

    elif text == "❌ Remove User":
        await state.set_state(UserState.CHOOSING_USER_TO_REMOVE)
        allowed_users = list_allowed_users()
        if not allowed_users:
            await message.answer("There are no users on the allowed list to remove.", reply_markup=get_admin_keyboard())
            await state.set_state(UserState.ADMIN_PANEL)
            return
            
        keyboard = create_user_selection_keyboard(allowed_users, "remove")
        await message.answer("Select a user to remove:", reply_markup=keyboard)

    elif text == "📋 List Allowed Users":
        await listusers_command(message)
    elif text == "👥 List All Users":
        await userlist_command(message)
    elif text == "⬅️ Back to Main Menu":
        await state.set_state(UserState.IDLE)
        await message.answer("⬅️ Returning to the main menu.", reply_markup=get_main_keyboard(message.from_user.id))

async def handle_allow_user_callback(callback_query: CallbackQuery, state: FSMContext):
    user_id_to_add = int(callback_query.data.split(":")[1])
    user_to_add = get_user_by_id(user_id_to_add)
    
    if user_to_add and add_allowed_user_from_user(user_to_add):
        await callback_query.message.edit_text(f"✅ User <b>{user_to_add.get('first_name')}</b> (<code>{user_id_to_add}</code>) has been allowed.")
    else:
        await callback_query.message.edit_text(f"❌ Failed to allow user <code>{user_id_to_add}</code>.")
    await callback_query.answer()
    await state.set_state(UserState.ADMIN_PANEL)

async def handle_remove_user_callback(callback_query: CallbackQuery, state: FSMContext):
    user_id_to_remove = int(callback_query.data.split(":")[1])
    user_to_remove = get_user_by_id(user_id_to_remove)
    
    if user_to_remove and remove_allowed_user(user_id_to_remove):
        await callback_query.message.edit_text(f"🗑 User <b>{user_to_remove.get('first_name')}</b> (<code>{user_id_to_remove}</code>) has been removed.")
    else:
        await callback_query.message.edit_text(f"❌ Failed to remove user <code>{user_id_to_remove}</code>.")
    await callback_query.answer()
    await state.set_state(UserState.ADMIN_PANEL)

async def handle_admin_cancel_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    await callback_query.answer("Cancelled.")
    await state.set_state(UserState.ADMIN_PANEL)

