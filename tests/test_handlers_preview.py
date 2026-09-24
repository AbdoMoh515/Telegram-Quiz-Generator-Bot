"""Handler flow tests with fakes (preview/confirm/collect + file guards)."""

import asyncio
import os

import handlers
from states import UserState

MIXED = """1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c

2. Who wrote the novel 1984?
Answer: George Orwell"""


class FakeUser:
    def __init__(self, uid=1):
        self.id = uid
        self.username = "tester"
        self.first_name = "Test"


class FakeChat:
    def __init__(self, cid=7):
        self.id = cid


class FakeMessage:
    def __init__(self, text="", user_id=1, chat_id=7, bot=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id)
        self.bot = bot
        self.message_id = 1
        self.replies = []
        self.answers = []
        self.edits = []
        self.docs = []
        self.deleted = False

    async def reply(self, text, reply_markup=None):
        self.replies.append({"text": text, "reply_markup": reply_markup})
        return FakeMessage(bot=self.bot)

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append({"text": text, "reply_markup": reply_markup})
        return FakeMessage(bot=self.bot)

    async def edit_text(self, text):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True

    async def answer_document(self, document, caption=None):
        self.docs.append({"document": document, "caption": caption})


class FakeCallback:
    def __init__(self, data, message=None, user_id=1):
        self.data = data
        self.message = message or FakeMessage(user_id=user_id)
        self.from_user = FakeUser(user_id)
        self.notices = []

    async def answer(self, text="", show_alert=False):
        self.notices.append({"text": text, "alert": show_alert})


class FakeState:
    def __init__(self):
        self._state = None
        self._data = {}

    async def get_state(self):
        return self._state

    async def set_state(self, value):
        self._state = value

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def clear(self):
        self._state = None
        self._data = {}


class FakeBot:
    def __init__(self):
        self.polls = []
        self.messages = []

    async def send_poll(self, **kwargs):
        self.polls.append(kwargs)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)

    async def edit_message_text(self, *args, **kwargs):
        pass

    async def download(self, document, destination):
        with open(destination, "w", encoding="utf-8") as f:
            f.write(MIXED)


def _preview_token_from(msg):
    markup = msg.replies[-1]["reply_markup"]
    buttons = markup.inline_keyboard
    data = buttons[0][0].callback_data
    assert data.startswith("confirm_send:")
    return data.split(":", 1)[1]


def test_paste_goes_to_preview_not_sent():
    state = FakeState()
    asyncio.run(state.set_state(UserState.WAITING_FOR_FILE))
    bot = FakeBot()
    msg = FakeMessage(text=MIXED, bot=bot)
    asyncio.run(handlers.handle_text_message(msg, state))
    assert asyncio.run(state.get_state()) == UserState.AWAITING_CONFIRMATION
    assert "Preview" in msg.replies[-1]["text"]
    assert bot.polls == [] and bot.messages == []  # nothing dispatched yet
    data = asyncio.run(state.get_data())
    assert len(data["preview_questions"]) == 2
    assert data["preview_token"] == _preview_token_from(msg)


def test_invalid_paste_reports_and_resets():
    state = FakeState()
    asyncio.run(state.set_state(UserState.WAITING_FOR_FILE))
    msg = FakeMessage(text="just chatting, no format here")
    asyncio.run(handlers.handle_text_message(msg, state))
    assert "No valid questions" in msg.replies[-1]["text"]
    assert asyncio.run(state.get_state()) == UserState.IDLE


def test_confirm_sends_once_then_rejects_repeat():
    state = FakeState()
    msg = FakeMessage(text=MIXED)
    asyncio.run(handlers.process_quiz_extraction(msg, state, MIXED))
    token = _preview_token_from(msg)
    bot = FakeBot()
    cb = FakeCallback(data=f"confirm_send:{token}", message=msg)
    asyncio.run(handlers.confirm_send_callback(cb, state, bot))
    assert len(bot.polls) == 1 and len(bot.messages) == 1
    assert asyncio.run(state.get_state()) == UserState.IDLE
    # Repeat with the same (now consumed) token: no second dispatch.
    asyncio.run(handlers.confirm_send_callback(cb, state, bot))
    assert len(bot.polls) == 1 and len(bot.messages) == 1
    assert any("Already processed" in n["text"] or "Nothing to confirm" in n["text"]
               for n in cb.notices)


def test_stale_token_does_not_dispatch():
    state = FakeState()
    msg = FakeMessage(text=MIXED)
    asyncio.run(handlers.process_quiz_extraction(msg, state, MIXED))
    bot = FakeBot()
    cb = FakeCallback(data="confirm_send:deadbeef", message=msg)
    asyncio.run(handlers.confirm_send_callback(cb, state, bot))
    assert bot.polls == [] and bot.messages == []
    assert any("stale" in n["text"] for n in cb.notices)


def test_cancel_sends_nothing_and_resets():
    state = FakeState()
    msg = FakeMessage(text=MIXED)
    asyncio.run(handlers.process_quiz_extraction(msg, state, MIXED))
    token = _preview_token_from(msg)
    cb = FakeCallback(data=f"cancel_send:{token}", message=msg)
    asyncio.run(handlers.cancel_send_callback(cb, state))
    assert asyncio.run(state.get_state()) == UserState.IDLE
    assert "cancelled" in msg.edits[-1].lower()


def test_collect_mode_finish_leads_to_preview():
    state = FakeState()
    asyncio.run(state.set_state(UserState.COLLECTING_TEXT))
    asyncio.run(state.update_data(collect_buffer=[]))
    asyncio.run(handlers.handle_text_message(FakeMessage(text=MIXED), state))
    asyncio.run(handlers.handle_text_message(
        FakeMessage(text="3. Extra one?\nAnswer: yes"), state))
    data = asyncio.run(state.get_data())
    assert len(data["collect_buffer"]) == 2
    preview_msg = FakeMessage()
    cb = FakeCallback(data="collect_finish", message=preview_msg)
    asyncio.run(handlers.collect_finish_callback(cb, state))
    assert asyncio.run(state.get_state()) == UserState.AWAITING_CONFIRMATION
    assert "Preview" in preview_msg.replies[-1]["text"]


def test_handle_file_rejects_pdf():
    state = FakeState()
    bot = FakeBot()
    bot.download_called = False

    async def _boom(document, destination):
        bot.download_called = True

    bot.download = _boom
    msg = FakeMessage(bot=bot)
    msg.document = type("Doc", (), {"file_name": "quiz.pdf"})()
    asyncio.run(handlers.handle_file(msg, state))
    assert not bot.download_called
    assert "Only" in msg.replies[-1]["text"] and ".txt" in msg.replies[-1]["text"]


def test_handle_file_txt_goes_to_preview():
    state = FakeState()
    bot = FakeBot()
    msg = FakeMessage(bot=bot)
    msg.document = type("Doc", (), {"file_name": "quiz.md"})()
    asyncio.run(handlers.handle_file(msg, state))
    assert asyncio.run(state.get_state()) == UserState.AWAITING_CONFIRMATION


class FakeOption:
    def __init__(self, text):
        self.text = text


class FakePoll:
    def __init__(self, question, options, correct_option_id=None):
        self.question = question
        self.options = [FakeOption(o) for o in options]
        self.correct_option_id = correct_option_id


def test_collect_cancel_stale_state_does_nothing():
    state = FakeState()
    asyncio.run(state.set_state(UserState.AWAITING_CONFIRMATION))
    asyncio.run(state.update_data(collect_buffer=["1. Q?\nAnswer: A"],
                                  preview_token="tok"))
    msg = FakeMessage()
    cb = FakeCallback(data="collect_cancel", message=msg)
    asyncio.run(handlers.collect_cancel_callback(cb, state))
    assert asyncio.run(state.get_state()) == UserState.AWAITING_CONFIRMATION
    assert any("not active" in n["text"] for n in cb.notices)
    assert msg.edits == []
    data = asyncio.run(state.get_data())
    assert data["collect_buffer"] == ["1. Q?\nAnswer: A"]


def test_collect_cancel_in_collect_state_clears():
    state = FakeState()
    asyncio.run(state.set_state(UserState.COLLECTING_TEXT))
    asyncio.run(state.update_data(collect_buffer=["1. Q?\nAnswer: A"]))
    msg = FakeMessage()
    cb = FakeCallback(data="collect_cancel", message=msg)
    asyncio.run(handlers.collect_cancel_callback(cb, state))
    assert asyncio.run(state.get_state()) == UserState.IDLE
    assert "cancelled" in msg.edits[-1].lower()


def test_collect_finish_uses_callback_user_not_bot_message():
    state = FakeState()
    asyncio.run(state.set_state(UserState.COLLECTING_TEXT))
    asyncio.run(state.update_data(collect_buffer=["just chatting, no format"]))
    bot_msg = FakeMessage(user_id=999)  # callback message authored by the bot
    cb = FakeCallback(data="collect_finish", message=bot_msg, user_id=42)
    seen = {}
    orig = handlers.get_main_keyboard

    def _capture(uid):
        seen["uid"] = uid
        return orig(uid)

    handlers.get_main_keyboard = _capture
    try:
        asyncio.run(handlers.collect_finish_callback(cb, state))
    finally:
        handlers.get_main_keyboard = orig
    assert seen.get("uid") == 42
    assert "No valid questions" in bot_msg.replies[-1]["text"]
    assert asyncio.run(state.get_state()) == UserState.IDLE


def test_concurrent_confirm_sends_only_once():
    async def _run():
        state = FakeState()
        msg = FakeMessage(text=MIXED)
        await handlers.process_quiz_extraction(msg, state, MIXED)
        token = _preview_token_from(msg)
        bot = FakeBot()
        cb1 = FakeCallback(data=f"confirm_send:{token}", message=msg)
        cb2 = FakeCallback(data=f"confirm_send:{token}", message=msg)
        await asyncio.gather(
            handlers.confirm_send_callback(cb1, state, bot),
            handlers.confirm_send_callback(cb2, state, bot),
        )
        return bot, cb1, cb2, state

    bot, cb1, cb2, state = asyncio.run(_run())
    assert len(bot.polls) == 1 and len(bot.messages) == 1
    assert asyncio.run(state.get_state()) == UserState.IDLE
    notices = cb1.notices + cb2.notices
    assert any("Already processed" in n["text"] or "Nothing to confirm" in n["text"]
               for n in notices)


def test_forwarded_poll_export_unchanged():
    state = FakeState()
    asyncio.run(state.set_state(UserState.COLLECTING_QUIZZES))
    asyncio.run(state.update_data(
        quizzes=[
            FakePoll("Capital?", ["Giza", "Cairo"], 1),
            FakePoll("No answer poll?", ["A", "B"], None),
        ],
        quiz_buffer=[],
    ))
    msg = FakeMessage()
    cb = FakeCallback(data="finish_extraction", message=msg)
    asyncio.run(handlers.finish_extraction_callback(cb, state))
    assert len(msg.docs) == 1
    assert "Extracted 2" in msg.docs[0]["caption"]
    assert asyncio.run(state.get_state()) is None
    # No temp leftovers from the export.
    leftovers = [f for f in os.listdir("temp") if f.startswith("quiz_1")]
    assert leftovers == []
