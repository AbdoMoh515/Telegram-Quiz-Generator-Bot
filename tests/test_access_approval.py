"""Admin approval-on-/start tests (offline fakes, temp-dir isolated)."""

import asyncio
import contextlib
import os
import tempfile
import threading

import access_requests
import filedb
import handlers
import handlers_admin
from access_requests import build_access_request_text

ADMIN_A = 9001
ADMIN_B = 9002


@contextlib.contextmanager
def isolated_env(admin_ids):
    """Isolate temp DB, user fixtures and ADMIN_IDS (restored after)."""
    prev_tmp = os.environ.get("BOT_TEMP_DIR")
    prev_users = filedb.USERS_FILE
    prev_allowed = filedb.ALLOWED_USERS_FILE
    prev_cache = set(filedb._allowed_user_ids_cache)
    prev_h_admins = handlers.ADMIN_IDS
    prev_ha_admins = handlers_admin.ADMIN_IDS
    prev_locks = dict(handlers_admin._decision_locks)
    handlers_admin._decision_locks.clear()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BOT_TEMP_DIR"] = tmp
        filedb.USERS_FILE = os.path.join(tmp, "users.json")
        filedb.ALLOWED_USERS_FILE = os.path.join(tmp, "allowed_users.json")
        filedb._allowed_user_ids_cache = set()
        filedb.load_allowed_users_cache()
        handlers.ADMIN_IDS = list(admin_ids)
        handlers_admin.ADMIN_IDS = list(admin_ids)
        try:
            yield tmp
        finally:
            if prev_tmp is None:
                os.environ.pop("BOT_TEMP_DIR", None)
            else:
                os.environ["BOT_TEMP_DIR"] = prev_tmp
            filedb.USERS_FILE = prev_users
            filedb.ALLOWED_USERS_FILE = prev_allowed
            filedb._allowed_user_ids_cache = prev_cache
            handlers.ADMIN_IDS = prev_h_admins
            handlers_admin.ADMIN_IDS = prev_ha_admins
            handlers_admin._decision_locks.clear()
            handlers_admin._decision_locks.update(prev_locks)


class FakeUser:
    def __init__(self, uid, username="", first_name="", last_name=""):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class FakeSentMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeMessage:
    def __init__(self, user, bot=None, text=""):
        self.from_user = user
        self.chat = type("Chat", (), {"id": user.id})()
        self.bot = bot
        self.text = text
        self.message_id = 5
        self.replies = []
        self.answers = []
        self.edits = []

    async def reply(self, text, reply_markup=None):
        self.replies.append({"text": text, "reply_markup": reply_markup})
        return FakeSentMessage(6)

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append({"text": text, "reply_markup": reply_markup})
        return FakeSentMessage(6)

    async def edit_text(self, text):
        self.edits.append(text)


class FakeCallback:
    def __init__(self, data, user, message=None, bot=None):
        self.data = data
        self.from_user = user
        self.message = message or FakeMessage(user, bot=bot)
        self.bot = bot
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
        self.sent = []      # bot.send_message records
        self.edited = []    # bot.edit_message_text records

    async def send_message(self, chat_id, text=None, **kwargs):
        self.sent.append(
            {"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return FakeSentMessage(len(self.sent))

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edited.append(
            {"text": text, "chat_id": chat_id, "message_id": message_id})


def _start(uid=42, username="newuser", first="New", last="User", bot=None):
    user = FakeUser(uid, username, first, last)
    return FakeMessage(user, bot=bot, text="/start"), FakeState()


def test_request_text_escapes_untrusted_parts():
    text = build_access_request_text("<b>evil</b>&", 'A "Q" <i>x</i>', 123)
    assert "<b>evil</b>" not in text
    assert "&lt;b&gt;evil&lt;/b&gt;&amp;" in text
    assert "&lt;i&gt;" in text
    assert "<code>123</code>" in text
    # Optional username omitted entirely when absent.
    no_user = build_access_request_text("", "Full Name", 7)
    assert "Username" not in no_user
    assert "Full Name" in no_user
    assert "<code>7</code>" in no_user


def test_start_unapproved_notifies_each_admin_once():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=42, username="newuser",
                            first="New", last="User", bot=bot)
        await handlers.start_command(msg, state)
        admin_dms = [s for s in bot.sent
                     if s["chat_id"] in (ADMIN_A, ADMIN_B)]
        assert len(admin_dms) == 2
        for dm in admin_dms:
            assert "@newuser" in dm["text"]
            assert "New User" in dm["text"]
            assert "<code>42</code>" in dm["text"]
            markup = dm["kwargs"]["reply_markup"]
            callbacks = [b.callback_data for row in markup.inline_keyboard
                         for b in row]
            assert "approve:42" in callbacks
            assert "reject:42" in callbacks
        assert any("access request has been sent" in r["text"].lower()
                   for r in msg.replies)
        row = access_requests.get_request(42)
        assert row and row["status"] == "pending"

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_repeat_start_while_pending_does_not_spam_admins():
    async def _main():
        bot = FakeBot()
        msg, state = _start(bot=bot)
        await handlers.start_command(msg, state)
        assert len(bot.sent) == 2
        msg2, state2 = _start(bot=bot)
        await handlers.start_command(msg2, state2)
        assert len(bot.sent) == 2  # no new admin DMs
        assert any("still pending" in r["text"] for r in msg2.replies)

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_approve_flow_allows_user_and_retires_buttons():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=42, bot=bot)
        await handlers.start_command(msg, state)
        admin = FakeUser(ADMIN_A)
        cb = FakeCallback("approve:42", admin, bot=bot)
        await handlers_admin.handle_approve_callback(cb, bot)
        assert filedb.is_user_allowed(42)
        # User got decision feedback.
        assert any(s["chat_id"] == 42 and "approved" in s["text"].lower()
                   for s in bot.sent)
        # Both admin notifications retired (buttons gone, outcome shown).
        assert len(bot.edited) == 2
        assert all("Approved" in e["text"] for e in bot.edited)
        assert cb.notices[0]["text"] == "User approved."
        # Second admin tapping afterwards is idempotent, no duplicates.
        cb2 = FakeCallback(
            "approve:42", FakeUser(ADMIN_B),
            message=FakeMessage(FakeUser(ADMIN_B), bot=bot), bot=bot)
        await handlers_admin.handle_approve_callback(cb2, bot)
        assert any("Already resolved" in n["text"] for n in cb2.notices)
        assert len(filedb.list_allowed_users()) == 1
        # Approved users can use the bot: no new request on /start.
        bot.sent.clear()
        msg3, state3 = _start(uid=42, bot=bot)
        await handlers.start_command(msg3, state3)
        assert bot.sent == []
        assert any("Welcome" in a["text"] for a in msg3.answers)

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_concurrent_approvals_single_winner():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=77, bot=bot)
        await handlers.start_command(msg, state)

        async def _tap(admin_id):
            cb = FakeCallback(
                "approve:77", FakeUser(admin_id),
                message=FakeMessage(FakeUser(admin_id), bot=bot), bot=bot)
            await handlers_admin.handle_approve_callback(cb, bot)
            return cb

        cb_a, cb_b = await asyncio.gather(_tap(ADMIN_A), _tap(ADMIN_B))
        notices = [n["text"] for n in cb_a.notices + cb_b.notices]
        assert sum("User approved." in n for n in notices) == 1
        assert sum("Already resolved" in n for n in notices) == 1
        assert len(filedb.list_allowed_users()) == 1
        assert filedb.is_user_allowed(77)

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_reject_flow_and_repeat_start_stays_rejected():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=55, bot=bot)
        await handlers.start_command(msg, state)
        assert len(bot.sent) == 2
        cb = FakeCallback("reject:55", FakeUser(ADMIN_B), bot=bot)
        await handlers_admin.handle_reject_callback(cb, bot)
        assert not filedb.is_user_allowed(55)
        assert any(s["chat_id"] == 55 and "declined" in s["text"].lower()
                   for s in bot.sent)
        assert all("Rejected" in e["text"] for e in bot.edited)
        # Repeat /start: declined notice, admins NOT re-notified.
        before = len(bot.sent)
        msg2, state2 = _start(uid=55, bot=bot)
        await handlers.start_command(msg2, state2)
        assert len(bot.sent) == before
        assert any("declined" in r["text"].lower()
                   for r in msg2.replies)
        # A later reject tap is a harmless idempotent no-op.
        cb2 = FakeCallback(
            "reject:55", FakeUser(ADMIN_A),
            message=FakeMessage(FakeUser(ADMIN_A), bot=bot), bot=bot)
        await handlers_admin.handle_reject_callback(cb2, bot)
        assert any("Already resolved" in n["text"] for n in cb2.notices)

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_non_admin_callback_denied_and_stale_click():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=42, bot=bot)
        await handlers.start_command(msg, state)
        intruder = FakeUser(4242, "intruder")
        cb = FakeCallback("approve:42", intruder, bot=bot)
        await handlers_admin.handle_approve_callback(cb, bot)
        assert any("Admins only" in n["text"] for n in cb.notices)
        assert not filedb.is_user_allowed(42)
        assert access_requests.get_request(42)["status"] == "pending"
        # Stale click for an unknown user.
        stale = FakeCallback("reject:999", FakeUser(ADMIN_A), bot=bot)
        await handlers_admin.handle_reject_callback(stale, bot)
        assert any("no longer exists" in n["text"] for n in stale.notices)

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_no_request_for_admins_and_approved_users():
    async def _main():
        bot = FakeBot()
        # Admin /start: welcome, no request, no DMs.
        admin_msg, admin_state = _start(uid=ADMIN_A, username="boss",
                                       first="Boss", last="", bot=bot)
        await handlers.start_command(admin_msg, admin_state)
        assert any("Welcome" in a["text"] for a in admin_msg.answers)
        assert bot.sent == []
        assert access_requests.get_request(ADMIN_A) is None
        # Manually allowed user: welcome, no request, no DMs.
        filedb.add_allowed_user_from_user(
            {"id": 1, "username": "ok", "first_name": "Ok"})
        filedb.load_allowed_users_cache()
        msg, state = _start(uid=1, username="ok", first="Ok", bot=bot)
        await handlers.start_command(msg, state)
        assert any("Welcome" in a["text"] for a in msg.answers)
        assert bot.sent == []
        assert access_requests.get_request(1) is None

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_upsert_refreshes_names_preserves_fields():
    with isolated_env([ADMIN_A]):
        assert filedb.upsert_user(5, "oldname", "Old", "Last")
        first_row = filedb.get_user_by_id(5)
        joined = first_row["date_joined"]
        assert first_row["last_name"] == "Last"
        assert filedb.upsert_user(5, "newname", "New", "NewLast")
        row = filedb.get_user_by_id(5)
        assert row["username"] == "newname"
        assert row["first_name"] == "New"
        assert row["last_name"] == "NewLast"
        assert row["date_joined"] == joined  # preserved
        # Old 3-argument calls keep working.
        assert filedb.upsert_user(6, "six", "Six")
        assert filedb.get_user_by_id(6)["username"] == "six"


def test_approve_write_failure_is_retriable_single_dm():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=42, bot=bot)
        await handlers.start_command(msg, state)
        real_add = handlers_admin.add_allowed_user_from_user
        attempts = []

        def flaky(user):
            attempts.append(user["id"])
            if len(attempts) == 1:
                raise OSError("synthetic allowed-list write failure")
            return real_add(user)

        handlers_admin.add_allowed_user_from_user = flaky
        try:
            cb = FakeCallback("approve:42", FakeUser(ADMIN_A), bot=bot)
            await handlers_admin.handle_approve_callback(cb, bot)
        finally:
            handlers_admin.add_allowed_user_from_user = real_add
        # The race was won but the write failed: the request is pending
        # again (not stuck approved), the user is still unauthorized,
        # nobody was DM'd, and the admin is told to retry.
        assert not filedb.is_user_allowed(42)
        assert access_requests.get_request(42)["status"] == "pending"
        assert not [s for s in bot.sent if s["chat_id"] == 42]
        assert any("re-opened" in n["text"] for n in cb.notices)
        # A retry wins cleanly with exactly one user DM.
        cb2 = FakeCallback(
            "approve:42", FakeUser(ADMIN_B),
            message=FakeMessage(FakeUser(ADMIN_B), bot=bot), bot=bot)
        await handlers_admin.handle_approve_callback(cb2, bot)
        assert filedb.is_user_allowed(42)
        user_dms = [s for s in bot.sent
                    if s["chat_id"] == 42 and "approved" in s["text"].lower()]
        assert len(user_dms) == 1
        assert cb2.notices[0]["text"] == "User approved."
        assert len(filedb.list_allowed_users()) == 1

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_manual_remove_rearms_request_and_start_renotifies():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=42, bot=bot)
        await handlers.start_command(msg, state)
        cb = FakeCallback("approve:42", FakeUser(ADMIN_A), bot=bot)
        await handlers_admin.handle_approve_callback(cb, bot)
        assert filedb.is_user_allowed(42)
        # Manual remove via the admin panel reconciles the request row.
        rm_cb = FakeCallback("remove:42", FakeUser(ADMIN_A), bot=bot)
        await handlers_admin.handle_remove_user_callback(rm_cb, FakeState())
        assert not filedb.is_user_allowed(42)
        assert access_requests.get_request(42)["status"] == "expired"
        # Next /start files a FRESH request: admins re-notified, no
        # welcome for the now-unauthorized user.
        bot.sent.clear()
        msg2, state2 = _start(uid=42, bot=bot)
        await handlers.start_command(msg2, state2)
        admin_dms = [s for s in bot.sent
                     if s["chat_id"] in (ADMIN_A, ADMIN_B)]
        assert len(admin_dms) == 2
        assert not msg2.answers
        assert any("access request has been sent" in r["text"].lower()
                   for r in msg2.replies)

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_start_with_stale_approved_record_renotifies():
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=42, bot=bot)
        await handlers.start_command(msg, state)
        cb = FakeCallback("approve:42", FakeUser(ADMIN_A), bot=bot)
        await handlers_admin.handle_approve_callback(cb, bot)
        assert filedb.is_user_allowed(42)
        # Overlapping change that bypassed the request record (e.g. a
        # direct allowed-list edit): approved row, unauthorized user.
        assert filedb.remove_allowed_user(42)
        assert access_requests.get_request(42)["status"] == "approved"
        bot.sent.clear()
        msg2, state2 = _start(uid=42, bot=bot)
        await handlers.start_command(msg2, state2)
        # The stale record never welcomes: admins get a fresh request.
        admin_dms = [s for s in bot.sent
                     if s["chat_id"] in (ADMIN_A, ADMIN_B)]
        assert len(admin_dms) == 2
        assert not msg2.answers
        assert any("access request has been sent" in r["text"].lower()
                   for r in msg2.replies)
        assert access_requests.get_request(42)["status"] == "pending"

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_gated_write_race_failure_then_retry_single_dm():
    """A tap racing a gated failing write must not retire live buttons.

    Admin A wins the resolve race but blocks inside the allowed-list
    write; admin B tapping meanwhile must WAIT (per-user lock) rather
    than see "already" and retire both admin cards. When A's write
    fails, the request re-opens with buttons still live, B's waiting
    tap retries the write itself, exactly one user DM is ever sent,
    and both cards retire exactly once with the success outcome.
    """
    async def _main():
        bot = FakeBot()
        msg, state = _start(uid=42, bot=bot)
        await handlers.start_command(msg, state)
        assert len(bot.sent) == 2  # both admin cards live

        real_add = handlers_admin.add_allowed_user_from_user
        calls = []
        started = threading.Event()
        proceed = threading.Event()

        def gated(user):
            calls.append(user["id"])
            if len(calls) == 1:
                started.set()
                assert proceed.wait(timeout=10), \
                    "timed out waiting for test release"
                raise OSError("synthetic gated write failure")
            return real_add(user)

        handlers_admin.add_allowed_user_from_user = gated
        try:
            async def _tap(admin_id):
                cb = FakeCallback(
                    "approve:42", FakeUser(admin_id),
                    message=FakeMessage(FakeUser(admin_id), bot=bot),
                    bot=bot)
                await handlers_admin.handle_approve_callback(cb, bot)
                return cb

            task_a = asyncio.create_task(_tap(ADMIN_A))
            # Wait until A is blocked inside the write (in a worker
            # thread -- the event loop stays free for B).
            await asyncio.to_thread(started.wait, 10)
            assert started.is_set()
            await asyncio.sleep(0.1)  # let A settle into the write
            task_b = asyncio.create_task(_tap(ADMIN_B))
            await asyncio.sleep(0.2)  # B must be waiting, not retiring
            # No premature retire while the write is still gated: both
            # admin cards stay live and nobody was DM'd yet.
            assert bot.edited == [], bot.edited
            assert not [s for s in bot.sent if s["chat_id"] == 42]
            assert access_requests.get_request(42)["status"] == "approved"
            proceed.set()  # let A's write fail now
            cb_a, cb_b = await asyncio.gather(task_a, task_b)
        finally:
            handlers_admin.add_allowed_user_from_user = real_add

        # A failed and re-opened; B retried on the live request and won.
        assert any("re-opened" in n["text"] for n in cb_a.notices)
        assert cb_b.notices[0]["text"] == "User approved."
        assert filedb.is_user_allowed(42)
        user_dms = [s for s in bot.sent
                    if s["chat_id"] == 42 and "approved" in s["text"].lower()]
        assert len(user_dms) == 1  # exactly one DM across both taps
        # Both admin cards retired exactly once, with the outcome.
        assert len(bot.edited) == 2
        assert all("Approved" in e["text"] for e in bot.edited)
        assert access_requests.get_request(42)["status"] == "approved"

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())


def test_middleware_public_commands_and_gate():
    from aiogram.types import Message as TgMessage
    from aiogram.types import CallbackQuery as TgCallback

    class M(TgMessage):
        def __init__(self, text):
            super().__init__()
            self.text = text
            self.replied = []
            self.document = None

        async def reply(self, text):
            self.replied.append(text)

    class C(TgCallback):
        def __init__(self):
            super().__init__()
            self.notices = []

        async def answer(self, text="", show_alert=False):
            self.notices.append(text)

    async def _main():
        calls = []

        async def _handler(event, data):
            calls.append(event)
            return "handled"

        middleware = handlers_admin.AccessControlMiddleware()
        stranger = FakeUser(31337, "stranger")
        # Public commands pass for unauthorized users.
        for cmd in ("/start", "/help", "/myaccess"):
            calls.clear()
            result = await middleware(
                _handler, M(cmd), {"event_from_user": stranger})
            assert result == "handled"
        # Anything else is blocked with a reply.
        calls.clear()
        blocked = M("📝 Create Quiz")
        result = await middleware(
            _handler, blocked, {"event_from_user": stranger})
        assert result is None and calls == []
        assert blocked.replied and "Access Denied" in blocked.replied[0]
        # Callbacks from strangers are answered, not handled.
        calls.clear()
        cb = C()
        result = await middleware(
            _handler, cb, {"event_from_user": stranger})
        assert result is None and calls == []
        assert any("Access Denied" in n for n in cb.notices)
        # Allowed users and admins pass through.
        filedb.add_allowed_user_from_user(
            {"id": 50, "username": "ok", "first_name": "Ok"})
        filedb.load_allowed_users_cache()
        calls.clear()
        assert await middleware(
            _handler, M("hello"), {"event_from_user": FakeUser(50)}) \
            == "handled"
        calls.clear()
        assert await middleware(
            _handler, M("hello"),
            {"event_from_user": FakeUser(ADMIN_A)}) == "handled"

    with isolated_env([ADMIN_A, ADMIN_B]):
        asyncio.run(_main())
