from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Dict
from config import ADMIN_IDS

def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Create the main keyboard, adding admin and utility buttons."""
    keyboard_buttons = [
        [KeyboardButton(text="📝 Create Quiz")],
        [KeyboardButton(text="🧩 Collect Messages")],
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


def get_preview_keyboard(token: str) -> InlineKeyboardMarkup:
    """Send/Cancel controls for the parsed-question preview step."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Send", callback_data=f"confirm_send:{token}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_send:{token}")],
    ])


def get_collect_keyboard() -> InlineKeyboardMarkup:
    """Finish/Cancel controls for the collect-messages mode."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Finish", callback_data="collect_finish")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="collect_cancel")],
    ])


def get_access_request_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Approve/Reject controls DM'd to admins for an access request."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{user_id}")],
        [InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{user_id}")],
    ])

