"""Offline test bootstrap: stub aiogram/config/filedb/handlers_admin.

aiogram (and python-dotenv-backed config) are not installed in this
environment, so lightweight stub modules are registered in sys.modules
before the real bot modules are imported. At runtime with aiogram
installed the real packages are used instead.
"""

import os
import sys
import types as _stdlib_types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install(name, **attrs):
    mod = _stdlib_types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _Holder:
    def __init__(self, *args, **kwargs):
        self.__dict__.update(kwargs)
        self.args = args


class _MessageHolder(_Holder):
    """Distinct stub so isinstance(x, Message) is False for callbacks."""


class _CallbackQueryHolder(_Holder):
    """Distinct stub so isinstance(x, CallbackQuery) is False for messages."""


def _ensure_aiogram_stubs():
    try:
        import aiogram  # noqa: F401
        return
    except Exception:
        pass

    class TelegramBadRequest(Exception):
        def __init__(self, *args, **kwargs):
            super().__init__(args[0] if args else "")
            self.kwargs = kwargs

    class State:
        pass

    class StatesGroup:
        pass

    class FSMContext:
        pass

    class Bot:
        pass

    types_mod = _install(
        "aiogram.types",
        Message=_MessageHolder,
        CallbackQuery=_CallbackQueryHolder,
        FSInputFile=_Holder,
        Poll=_Holder,
        InlineKeyboardMarkup=_Holder,
        InlineKeyboardButton=_Holder,
        ReplyKeyboardMarkup=_Holder,
        KeyboardButton=_Holder,
    )
    aiogram_mod = _install(
        "aiogram", types=types_mod, Bot=Bot, BaseMiddleware=object,
    )
    aiogram_mod.types = types_mod
    fsm_mod = _install("aiogram.fsm")
    _install("aiogram.fsm.state", State=State, StatesGroup=StatesGroup)
    _install("aiogram.fsm.context", FSMContext=FSMContext)
    _install("aiogram.exceptions", TelegramBadRequest=TelegramBadRequest)
    sys.modules["aiogram.fsm"] = fsm_mod


def _ensure_config_stub():
    if "config" in sys.modules:
        return
    try:
        import config  # noqa: F401
        return
    except Exception:
        pass
    _install(
        "config",
        TELEGRAM_TOKEN="test-token",
        LOG_CHANNEL_ID=0,
        ADMIN_IDS=[],
    )


def _ensure_filedb_stub():
    if "filedb" in sys.modules:
        return
    try:
        import filedb  # noqa: F401
        return
    except Exception:
        pass
    _install("filedb", upsert_user=lambda *a, **k: True)


async def _noop_admin_text(message, state):
    return None


def _ensure_handlers_admin_stub():
    if "handlers_admin" in sys.modules:
        return
    try:
        import handlers_admin  # noqa: F401
        return
    except Exception:
        pass
    _install("handlers_admin", handle_admin_text_message=_noop_admin_text)


_ensure_aiogram_stubs()
_ensure_config_stub()
_ensure_filedb_stub()
_ensure_handlers_admin_stub()
