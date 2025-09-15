from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Dict
from config import ADMIN_IDS

def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Create the main keyboard, adding admin and utility buttons."""
    keyboard_buttons = [
        [KeyboardButton(text="📝 Create Quiz")],
        [KeyboardButton(text="📥 Extract Quizzes from Forwards")],
        [KeyboardButton(text="❓ Help"), KeyboardButton(text="🤖 Get AI Prompt")]
    ]

    if user_id in ADMIN_IDS:
        keyboard_buttons.append([KeyboardButton(text="👑 Admin Panel")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard_buttons,
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_admin_keyboard() -> ReplyKeyboardMarkup:
    """Create the admin panel keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Allow User"), KeyboardButton(text="❌ Remove User")],
            [KeyboardButton(text="📋 List Allowed Users"), KeyboardButton(text="👥 List All Users")],
            [KeyboardButton(text="⬅️ Back to Main Menu")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def create_user_selection_keyboard(users: List[Dict], action_prefix: str) -> InlineKeyboardMarkup:
    """Dynamically creates a keyboard for selecting a user."""
    buttons = []
    for user in users:
        user_name = user.get('first_name') or user.get('username') or f"ID: {user['id']}"
        callback_data = f"{action_prefix}:{user['id']}"
        buttons.append([InlineKeyboardButton(text=user_name, callback_data=callback_data)])
    
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

