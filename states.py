from aiogram.fsm.state import State, StatesGroup

class UserState(StatesGroup):
    """
    Defines the states for a user's interaction with the bot.
    """
    IDLE = State()
    WAITING_FOR_FILE = State()
    COLLECTING_QUIZZES = State()
    COLLECTING_TEXT = State()
    AWAITING_CONFIRMATION = State()
    ADMIN_PANEL = State()
    CHOOSING_USER_TO_ALLOW = State()
    CHOOSING_USER_TO_REMOVE = State()

