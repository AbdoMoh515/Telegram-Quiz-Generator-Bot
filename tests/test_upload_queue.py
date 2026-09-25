"""Global upload FIFO queue + disk-backed streaming tests (offline fakes)."""

import asyncio
import contextlib
import errno
import inspect
import json
import os
import tempfile

import upload_queue
import handlers
from states import UserState
from utils import (
    extract_questions_from_text,
    parse_upload_file_to_disk,
)

MIXED = """1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c

2. Who wrote the novel 1984?
Answer: George Orwell"""


def mcq_block(i):
    return (f"{i}. Question number {i}?\na) Opt A{i}\nb) Opt B{i}\n"
            f"Answer: a")


def written_block(i):
    return f"{i}. Who is person {i}?\nAnswer: Name{i}"


@contextlib.contextmanager
def isolated_tmp():
    """Point BOT_TEMP_DIR at a fresh dir (tests never touch repo temp/)."""
    previous = os.environ.get("BOT_TEMP_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BOT_TEMP_DIR"] = tmp
        try:
            yield tmp
        finally:
            if previous is None:
                os.environ.pop("BOT_TEMP_DIR", None)
            else:
                os.environ["BOT_TEMP_DIR"] = previous


class FakeUser:
    def __init__(self, uid=1):
        self.id = uid
        self.username = "tester"
        self.first_name = "Test"
        self.last_name = ""


class FakeChat:
    def __init__(self, cid=7):
        self.id = cid


class FakeDoc:
    def __init__(self, file_name="quiz.md", file_id="fid"):
        self.file_name = file_name
        self.file_id = file_id


class FakeMessage:
    def __init__(self, text="", user_id=1, chat_id=7, bot=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id)
        self.bot = bot
        self.document = None
        self.message_id = 1
        self.replies = []
        self.answers = []
        self.edits = []
        self.docs = []

    async def reply(self, text, reply_markup=None):
        self.replies.append({"text": text, "reply_markup": reply_markup})
        return FakeMessage(bot=self.bot)

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append({"text": text, "reply_markup": reply_markup})
        return FakeMessage(bot=self.bot)

    async def edit_text(self, text):
        self.edits.append(text)

    async def answer_document(self, document, caption=None):
        self.docs.append({"document": document, "caption": caption})


class FakeCallback:
    def __init__(self, data, message=None, user_id=1):
        self.data = data
        self.message = message or FakeMessage(user_id=user_id)
        self.from_user = FakeUser(user_id)
        self.bot = self.message.bot
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
    def __init__(self, contents=None):
        self.contents = dict(contents or {})
        self.default_content = MIXED
        self.polls = []
        self.messages = []

    async def download(self, file_id, destination=None):
        content = self.contents.get(file_id, self.default_content)
        if isinstance(content, BaseException):
            raise content
        with open(destination, "w", encoding="utf-8") as f:
            f.write(content)

    async def send_message(self, chat_id, text=None, **kwargs):
        self.messages.append(
            {"chat_id": chat_id, "text": text, "kwargs": kwargs})
        sent = FakeMessage(bot=self)
        sent.message_id = len(self.messages)
        return sent

    async def send_poll(self, **kwargs):
        self.polls.append(kwargs)

    async def edit_message_text(self, *args, **kwargs):
        pass


def _presenter_factory(captured):
    async def _present(bot, chat_id, text, token):
        captured["chat_id"] = chat_id
        captured["text"] = text
        captured["token"] = token
        return FakeMessage(bot=bot)
    return _present


async def _wait_for(predicate, timeout=5.0):
    waited = 0.0
    while not predicate():
        if waited >= timeout:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.05)
        waited += 0.05


def _fifo_job_ids():
    """Job ids on disk oldest-first (enqueue order)."""
    try:
        names = os.listdir(upload_queue.jobs_root())
    except OSError:
        return []
    manifests = [upload_queue.get_job(n) for n in names]
    manifests = [m for m in manifests if m]
    manifests.sort(key=lambda m: m.get("created_at", 0))
    return [m["job_id"] for m in manifests]


def _job_id_for_file_id(file_id):
    for job_id in _fifo_job_ids():
        manifest = upload_queue.get_job(job_id)
        if manifest and manifest.get("file_id") == file_id:
            return job_id
    return None


def test_enqueue_is_fifo():
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        captured = {}

        async def _present(bot, chat_id, text, token):
            captured.setdefault("tokens", []).append(token)
            return FakeMessage(bot=bot)

        states = {}
        for uid in (11, 22, 33):
            state = FakeState()
            states[uid] = state
            msg = FakeMessage(bot=bot, user_id=uid, chat_id=uid)
            msg.document = FakeDoc(file_id=f"fid-{uid}")
            await handlers.handle_file(msg, state)
            # No unbounded per-upload list in FSM: each preview is
            # tracked by its own small job id / token pair on delivery.
            assert "pending_upload_ids" not in (await state.get_data())
        enqueued = _fifo_job_ids()
        assert len(enqueued) == 3

        worker = asyncio.create_task(upload_queue._worker_loop(
            bot, presenter=_present,
            fsm_factory=lambda c, u: states[u],
            awaiting_state=UserState.AWAITING_CONFIRMATION))
        try:
            await asyncio.wait_for(
                upload_queue._get_queue().join(), timeout=10)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        # FIFO start order matches enqueue order.
        assert upload_queue._start_order == enqueued
        assert len(captured["tokens"]) == 3

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_max_two_concurrent_downloads():
    async def _main():
        upload_queue.reset_for_tests()
        release = asyncio.Event()
        starts = []
        bot = FakeBot()

        async def gated_download(file_id, destination):
            starts.append(file_id)
            await release.wait()
            with open(destination, "w", encoding="utf-8") as f:
                f.write(MIXED)

        bot.download = gated_download
        captured = {}
        workers = upload_queue.start_workers(
            bot, count=2, presenter=_presenter_factory(captured))
        try:
            for i in (1, 2, 3):
                state = FakeState()
                msg = FakeMessage(bot=bot, user_id=i, chat_id=i)
                msg.document = FakeDoc(file_id=f"fid-{i}")
                await handlers.handle_file(msg, state)
            await _wait_for(lambda: len(starts) >= 2)
            # Only two workers active; the third upload waits fairly.
            assert upload_queue.active_count() == 2
            assert upload_queue.peak_active() <= 2
            assert set(starts) == {"fid-1", "fid-2"}
            release.set()
            await asyncio.wait_for(
                upload_queue._get_queue().join(), timeout=10)
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        assert upload_queue.peak_active() <= 2
        assert starts[:2] == ["fid-1", "fid-2"] or \
            set(starts[:2]) == {"fid-1", "fid-2"}
        assert starts[2] == "fid-3"

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_disk_backed_preview_and_fsm_ids_only():
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        state = FakeState()
        msg = FakeMessage(bot=bot)
        msg.document = FakeDoc(file_id="fid-1")
        await handlers.handle_file(msg, state)
        captured = {}
        await upload_queue.process_one(
            bot, presenter=_presenter_factory(captured),
            fsm_factory=lambda c, u: state,
            awaiting_state=UserState.AWAITING_CONFIRMATION)
        assert "Preview" in captured["text"]
        assert "Valid: <b>2</b>" in captured["text"]
        data = await state.get_data()
        # FSM holds only small identifiers -- never question records.
        assert data["preview_job_id"]
        assert data["preview_token"] == captured["token"]
        assert "preview_questions" not in data
        assert "extracted_questions" not in data
        assert os.path.exists(
            upload_queue.questions_path(data["preview_job_id"]))

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_large_file_incremental_matches_memory_parser():
    with isolated_tmp() as tmp:
        upload_queue.reset_for_tests()
        blocks = []
        for i in range(1, 1201):
            blocks.append(mcq_block(i) if i % 2 else written_block(i))
        text = "\n\n".join(blocks)
        src = os.path.join(tmp, "big.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
        job_dir = os.path.join(tmp, "job-big")
        summary = parse_upload_file_to_disk(src, job_dir)
        mem_questions, mem_skipped = extract_questions_from_text(text)
        assert summary["valid_count"] == len(mem_questions) == 1200
        assert summary["skipped_count"] == len(mem_skipped) == 0
        # JSONL holds every record; spot-check round-trip of a sample.
        with open(summary["questions_file"], encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1200
        import json as _json
        assert _json.loads(lines[0])["question_num"] == "1"


def test_oversized_block_skipped_with_reason():
    with isolated_tmp() as tmp:
        upload_queue.reset_for_tests()
        good = mcq_block(1)
        huge_answer = "x" * 5000
        bad = f"2. Oversized one?\nAnswer: {huge_answer}"
        # A mega-line with no trailing newline is bounded too.
        mega = "3. Mega line?\nAnswer: " + "z" * 5000
        src = os.path.join(tmp, "in.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write(good + "\n\n" + bad + "\n\n" + mega)
        summary = parse_upload_file_to_disk(src, os.path.join(tmp, "job"),
                                            block_limit=512)
        assert summary["valid_count"] == 1
        assert summary["skipped_count"] == 2
        with open(summary["skipped_file"], encoding="utf-8") as f:
            reasons = [json.loads(line)["reason"] for line in f if line.strip()]
        assert all("memory limit" in r for r in reasons)


def test_malformed_blocks_skipped_rest_parsed():
    with isolated_tmp() as tmp:
        upload_queue.reset_for_tests()
        text = ("1. No answer here?\na) Yes\nb) No\n\n"
                + mcq_block(2) + "\n\n"
                + "Just some prose, no number\n\n"
                + written_block(3))
        src = os.path.join(tmp, "in.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
        summary = parse_upload_file_to_disk(src, os.path.join(tmp, "job"))
        # The streaming parser must agree exactly with the in-memory one.
        mem_questions, mem_skipped = extract_questions_from_text(text)
        assert summary["valid_count"] == len(mem_questions) == 2
        assert summary["skipped_count"] == len(mem_skipped) == 1


def test_confirm_dispatches_from_disk_and_export_survives():
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        state = FakeState()
        msg = FakeMessage(bot=bot)
        msg.document = FakeDoc(file_id="fid-1")
        await handlers.handle_file(msg, state)
        captured = {}
        await upload_queue.process_one(
            bot, presenter=_presenter_factory(captured),
            fsm_factory=lambda c, u: state,
            awaiting_state=UserState.AWAITING_CONFIRMATION)
        token = captured["token"]
        job_id = (await state.get_data())["preview_job_id"]
        cb = FakeCallback(data=f"confirm_send:{token}", message=msg)
        await handlers.confirm_send_callback(cb, state, bot)
        # Dispatched from disk: 1 MCQ poll + 1 written spoiler message.
        assert len(bot.polls) == 1 and len(bot.messages) == 1
        assert await state.get_state() == UserState.IDLE
        data = await state.get_data()
        assert data["extracted_job_id"] == job_id
        assert os.path.exists(upload_queue.questions_path(job_id))
        # Repeat tap cannot re-send.
        await handlers.confirm_send_callback(cb, state, bot)
        assert len(bot.polls) == 1 and len(bot.messages) == 1
        assert any("Already processed" in n["text"] for n in cb.notices)
        # Show as Text streams the export from disk.
        show_msg = FakeMessage(bot=bot)
        show_cb = FakeCallback(data="show_questions", message=show_msg)
        await handlers.show_questions_callback(show_cb, state)
        assert len(show_msg.docs) == 1
        assert "2 extracted" in show_msg.docs[0]["caption"]
        # Post-send Cancel discards state and disk result.
        cancel_msg = FakeMessage(bot=bot)
        cancel_cb = FakeCallback(data="cancel_processing", message=cancel_msg)
        await handlers.cancel_processing_callback(cancel_cb, state)
        assert await state.get_data() == {}
        assert not os.path.exists(upload_queue.job_dir(job_id))

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_cancel_cleans_disk_and_state():
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        state = FakeState()
        msg = FakeMessage(bot=bot)
        msg.document = FakeDoc(file_id="fid-1")
        await handlers.handle_file(msg, state)
        captured = {}
        await upload_queue.process_one(
            bot, presenter=_presenter_factory(captured),
            fsm_factory=lambda c, u: state,
            awaiting_state=UserState.AWAITING_CONFIRMATION)
        job_id = (await state.get_data())["preview_job_id"]
        cb = FakeCallback(data=f"cancel_send:{captured['token']}",
                          message=msg)
        await handlers.cancel_send_callback(cb, state)
        assert await state.get_state() == UserState.IDLE
        assert not os.path.exists(upload_queue.job_dir(job_id))

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_download_too_big_and_disk_full_messages():
    async def _main():
        upload_queue.reset_for_tests()
        big_fail = Exception("Bad Request: file is too big")
        bot = FakeBot(contents={"fid-big": big_fail})
        state = FakeState()
        msg = FakeMessage(bot=bot)
        msg.document = FakeDoc(file_id="fid-big")
        await handlers.handle_file(msg, state)
        await upload_queue.process_one(bot, presenter=_presenter_factory({}))
        assert any("20 MB" in m["text"] for m in bot.messages)
        assert os.listdir(upload_queue.jobs_root()) == []

        disk_fail = OSError(errno.ENOSPC, "No space left on device")
        bot2 = FakeBot(contents={"fid-full": disk_fail})
        state2 = FakeState()
        msg2 = FakeMessage(bot=bot2)
        msg2.document = FakeDoc(file_id="fid-full")
        await handlers.handle_file(msg2, state2)
        await upload_queue.process_one(bot2, presenter=_presenter_factory({}))
        assert any("disk is full" in m["text"] for m in bot2.messages)
        assert os.listdir(upload_queue.jobs_root()) == []

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_restart_recovery_and_stale_preview():
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        state_a = FakeState()
        state_b = FakeState()
        states = {11: state_a, 22: state_b}
        msg_a = FakeMessage(bot=bot, user_id=11, chat_id=11)
        msg_a.document = FakeDoc(file_id="fid-a")
        msg_b = FakeMessage(bot=bot, user_id=22, chat_id=22)
        msg_b.document = FakeDoc(file_id="fid-b")
        await handlers.handle_file(msg_a, state_a)
        await handlers.handle_file(msg_b, state_b)
        job_a = _job_id_for_file_id("fid-a")
        job_b = _job_id_for_file_id("fid-b")
        assert job_a and job_b and job_a != job_b
        captured = {}

        async def _present(bot, chat_id, text, token):
            captured[chat_id] = token
            return FakeMessage(bot=bot)

        # Only job A gets processed before the "restart".
        await upload_queue.process_one(
            bot, presenter=_present,
            fsm_factory=lambda c, u: states[u],
            awaiting_state=UserState.AWAITING_CONFIRMATION)
        assert set(captured) == {11}

        # --- restart: all in-memory state is gone ---
        upload_queue.reset_for_tests()
        requeues, cleaned = await asyncio.to_thread(
            upload_queue.recover_spool)
        assert [m["job_id"] for m in requeues] == [job_b]
        assert upload_queue.reregister_previews() == 1
        assert cleaned == 0

        # Stale button from before the restart still confirms (fresh FSM:
        # MemoryStorage was lost, the disk registry is the truth).
        fresh_state = FakeState()
        cb = FakeCallback(data=f"confirm_send:{captured[11]}",
                          message=FakeMessage(bot=bot, user_id=11, chat_id=11),
                          user_id=11)
        await handlers.confirm_send_callback(cb, fresh_state, bot)
        assert len(bot.polls) == 1 and len(bot.messages) == 1
        assert (await fresh_state.get_data())["extracted_job_id"] == job_a

        # The never-started upload resumes from the spool.
        for manifest in requeues:
            upload_queue.requeue_job(manifest["job_id"])
        captured2 = {}
        await upload_queue.process_one(
            bot, presenter=_presenter_factory(captured2),
            fsm_factory=lambda c, u: state_b,
            awaiting_state=UserState.AWAITING_CONFIRMATION)
        assert "Preview" in captured2["text"]

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_purge_expired_keeps_queued():
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        # A dispatched job whose result lingers on disk (enqueued first so
        # the single process_one call below takes it).
        state_d = FakeState()
        msg_d = FakeMessage(bot=bot)
        msg_d.document = FakeDoc(file_id="fid-d")
        await handlers.handle_file(msg_d, state_d)
        # A queued job still waiting for its turn.
        state_q = FakeState()
        msg_q = FakeMessage(bot=bot)
        msg_q.document = FakeDoc(file_id="fid-q")
        await handlers.handle_file(msg_q, state_q)
        job_q = _job_id_for_file_id("fid-q")
        assert job_q
        await upload_queue.process_one(
            bot, presenter=_presenter_factory({}),
            fsm_factory=lambda c, u: state_d,
            awaiting_state=UserState.AWAITING_CONFIRMATION)
        job_d = (await state_d.get_data())["preview_job_id"]
        assert job_d != job_q
        token_d = (await state_d.get_data())["preview_token"]
        cb = FakeCallback(data=f"confirm_send:{token_d}", message=msg_d)
        await handlers.confirm_send_callback(cb, state_d, bot)
        assert len(bot.polls) == 1
        # Only the dispatched result is old.
        path = os.path.join(upload_queue.job_dir(job_d), "manifest.json")
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["updated_at"] = 0
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        removed = upload_queue.purge_expired(max_age=3600)
        assert removed == 1
        assert os.path.exists(upload_queue.job_dir(job_q))
        assert not os.path.exists(upload_queue.job_dir(job_d))

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_no_bare_full_file_read_and_no_handler_download():
    from utils import _stream_blocks_from_file
    assert ".read()" not in inspect.getsource(_stream_blocks_from_file)
    assert ".read()" not in inspect.getsource(parse_upload_file_to_disk)
    # The upload handler only enqueues metadata; downloads happen inside
    # worker slots.
    assert ".download(" not in inspect.getsource(handlers.handle_file)


def test_streaming_dedup_disk_backed_and_skips_duplicates():
    import utils as utils_mod
    # Duplicate tracking must not grow an in-memory set with the upload:
    # the streaming parser uses a job-local SQLite UNIQUE index, and the
    # tracker itself holds no per-question containers.
    assert "seen_texts = set()" not in inspect.getsource(
        utils_mod.parse_upload_file_to_disk)
    assert "= set()" not in inspect.getsource(utils_mod._DiskSeenTexts)
    with isolated_tmp() as tmp:
        seen = utils_mod._DiskSeenTexts(os.path.join(tmp, "probe.db"))
        try:
            assert "q" not in seen
            seen.add("q")
            assert "q" in seen
            assert 12345 not in seen  # non-text lookups are safe misses
            containers = [v for v in vars(seen).values()
                          if isinstance(v, (set, list, dict))]
            assert containers == []
        finally:
            seen.dispose()
        assert not os.path.exists(os.path.join(tmp, "probe.db"))

        blocks = [mcq_block(i) for i in range(1, 201)]
        # Same question texts under different numbers -> duplicates.
        blocks += [mcq_block(i - 200) for i in range(201, 401)]
        text = "\n\n".join(blocks)
        src = os.path.join(tmp, "in.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
        job = os.path.join(tmp, "job-dedup")
        summary = parse_upload_file_to_disk(src, job)
        assert summary["valid_count"] == 200
        assert summary["skipped_count"] == 200
        with open(summary["skipped_file"], encoding="utf-8") as f:
            reasons = [json.loads(line)["reason"] for line in f
                       if line.strip()]
        assert len(reasons) == 200
        assert all(r == "Duplicate question." for r in reasons)
        # The dedup index is cleaned up; only the JSONL results remain.
        assert not os.path.exists(os.path.join(job, "dedup.db"))
        assert sorted(os.listdir(job)) == ["questions.jsonl",
                                           "skipped.jsonl"]
        # The streaming parser still agrees exactly with the memory one.
        mem_questions, mem_skipped = extract_questions_from_text(text)
        assert len(mem_questions) == 200
        assert len(mem_skipped) == 200


def test_parse_failure_notifies_and_worker_recovers():
    async def _main():
        upload_queue.reset_for_tests()
        real_parse = upload_queue.parse_upload_file_to_disk
        calls = []

        def flaky(src, directory):
            calls.append(directory)
            if len(calls) == 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            if len(calls) == 2:
                raise RuntimeError("synthetic decoder blowup")
            return real_parse(src, directory)

        upload_queue.parse_upload_file_to_disk = flaky
        states = {}
        try:
            bot = FakeBot()
            for uid, fid in ((11, "fid-a"), (12, "fid-b"), (13, "fid-c")):
                state = FakeState()
                states[uid] = state
                msg = FakeMessage(bot=bot, user_id=uid, chat_id=uid)
                msg.document = FakeDoc(file_id=fid)
                await handlers.handle_file(msg, state)
            captured = {}
            for _ in range(3):
                await upload_queue.process_one(
                    bot, presenter=_presenter_factory(captured))
        finally:
            upload_queue.parse_upload_file_to_disk = real_parse
        by_chat = {}
        for m in bot.messages:
            by_chat.setdefault(m["chat_id"], []).append(m["text"])
        # ENOSPC gets the disk-full notice, other errors a clear notice.
        assert any("disk is full" in t for t in by_chat.get(11, [])), \
            by_chat
        assert any("Could not process your file" in t
                   for t in by_chat.get(12, [])), by_chat
        # Failed spools are cleaned; the queued job after them previews.
        assert _job_id_for_file_id("fid-a") is None
        assert _job_id_for_file_id("fid-b") is None
        assert "Preview" in captured.get("text", "")
        survivor = _job_id_for_file_id("fid-c")
        assert survivor
        assert os.path.exists(upload_queue.questions_path(survivor))

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_upload_token_bound_to_owner():
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        state = FakeState()
        msg = FakeMessage(bot=bot, user_id=1, chat_id=7)
        msg.document = FakeDoc(file_id="fid-1")
        await handlers.handle_file(msg, state)
        captured = {}
        await upload_queue.process_one(
            bot, presenter=_presenter_factory(captured),
            fsm_factory=lambda c, u: state,
            awaiting_state=UserState.AWAITING_CONFIRMATION)
        token = captured["token"]
        job_id = (await state.get_data())["preview_job_id"]
        # A foreign user cannot confirm: refused before any consume, so
        # nothing is dispatched and the token stays live for the owner.
        intruder = FakeCallback(data=f"confirm_send:{token}", message=msg,
                                user_id=2)
        await handlers.confirm_send_callback(intruder, FakeState(), bot)
        assert any("another user" in n["text"] for n in intruder.notices)
        assert bot.polls == [] and bot.messages == []
        assert upload_queue.peek_upload_token(token) == job_id
        # A foreign user cannot cancel either: the spooled job survives.
        intruder_cancel = FakeCallback(data=f"cancel_send:{token}",
                                       message=msg, user_id=2)
        await handlers.cancel_send_callback(intruder_cancel, FakeState())
        assert any("another user" in n["text"]
                   for n in intruder_cancel.notices)
        assert os.path.exists(upload_queue.job_dir(job_id))
        assert upload_queue.peek_upload_token(token) == job_id
        # The right user on the wrong chat message is refused too.
        elsewhere = FakeMessage(bot=bot, user_id=1, chat_id=999)
        cross = FakeCallback(data=f"confirm_send:{token}",
                             message=elsewhere, user_id=1)
        await handlers.confirm_send_callback(cross, FakeState(), bot)
        assert any("another chat" in n["text"] for n in cross.notices)
        assert bot.polls == []
        # The owner still confirms fine afterwards (single dispatch).
        owner = FakeCallback(data=f"confirm_send:{token}", message=msg,
                             user_id=1)
        await handlers.confirm_send_callback(owner, state, bot)
        assert len(bot.polls) == 1 and len(bot.messages) == 1

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_confirm_and_cancel_do_not_block_other_users():
    """A slow dispatch must not serialize ALL users' Send/Cancel.

    Regression: the global preview-claim lock used to be held across
    the whole 0.5s-per-question dispatch, so one large upload blocked
    every other user's confirm/cancel. Only the quick token claim may
    hold the lock now.
    """
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot()
        states = {}
        tokens = {}
        for uid, chat in ((1, 7), (2, 8), (3, 9)):
            state = FakeState()
            states[uid] = state
            msg = FakeMessage(bot=bot, user_id=uid, chat_id=chat)
            msg.document = FakeDoc(file_id=f"fid-{uid}")
            await handlers.handle_file(msg, state)
        for uid in (1, 2, 3):
            captured = {}
            await upload_queue.process_one(
                bot, presenter=_presenter_factory(captured),
                fsm_factory=lambda c, u, _s=states: _s[u],
                awaiting_state=UserState.AWAITING_CONFIRMATION)
            tokens[uid] = captured["token"]

        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        real_send = handlers.send_telegram_quizzes

        async def gated_send(bot, questions, chat_id, start_number=1):
            # Only user A's dispatch gates; everyone else flows freely.
            if chat_id == 7:
                calls.append(chat_id)
                entered.set()
                await release.wait()
            for _ in questions:
                pass
            if chat_id != 7:
                calls.append(chat_id)
            return (2, 0, [], start_number + 2)

        handlers.send_telegram_quizzes = gated_send
        try:
            msg_a = FakeMessage(bot=bot, user_id=1, chat_id=7)
            cb_a = FakeCallback(data=f"confirm_send:{tokens[1]}",
                                message=msg_a, user_id=1)
            task_a = asyncio.create_task(
                handlers.confirm_send_callback(cb_a, states[1], bot))
            await asyncio.wait_for(entered.wait(), timeout=5)

            # Another user's confirm must finish while A is dispatching.
            msg_b = FakeMessage(bot=bot, user_id=2, chat_id=8)
            cb_b = FakeCallback(data=f"confirm_send:{tokens[2]}",
                                message=msg_b, user_id=2)
            await asyncio.wait_for(
                handlers.confirm_send_callback(cb_b, states[2], bot),
                timeout=5)
            assert await states[2].get_state() == UserState.IDLE

            # A third user's cancel must also finish while A dispatches.
            msg_c = FakeMessage(bot=bot, user_id=3, chat_id=9)
            cb_c = FakeCallback(data=f"cancel_send:{tokens[3]}",
                                message=msg_c, user_id=3)
            await asyncio.wait_for(
                handlers.cancel_send_callback(cb_c, states[3]), timeout=5)
            assert await states[3].get_state() == UserState.IDLE

            release.set()
            await asyncio.wait_for(task_a, timeout=5)
            assert await states[1].get_state() == UserState.IDLE
            # Both confirms dispatched (A entered first, B overtook it).
            assert calls == [7, 8]
        finally:
            handlers.send_telegram_quizzes = real_send
            release.set()

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())


def test_repeated_uploads_do_not_grow_fsm():
    """handle_file must not accumulate an unbounded id list in FSM.

    Failed and empty results are cleaned without touching FSM, so
    uploading again and again (success, failure, empty) must leave
    only small bounded keys behind.
    """
    async def _main():
        upload_queue.reset_for_tests()
        bot = FakeBot(contents={
            "fid-ok": MIXED,
            "fid-boom": OSError(errno.ENOSPC, "No space left on device"),
            "fid-empty": "Just some prose, no numbers at all",
        })
        state = FakeState()
        for fid in ("fid-ok", "fid-boom", "fid-empty", "fid-ok",
                    "fid-boom"):
            msg = FakeMessage(bot=bot, user_id=1, chat_id=7)
            msg.document = FakeDoc(file_id=fid)
            await handlers.handle_file(msg, state)
            await upload_queue.process_one(
                bot, presenter=_presenter_factory({}),
                fsm_factory=lambda c, u: state,
                awaiting_state=UserState.AWAITING_CONFIRMATION)
        data = await state.get_data()
        assert "pending_upload_ids" not in data
        # Only small bounded identifiers, never question records.
        assert "preview_questions" not in data
        assert "extracted_questions" not in data
        assert len(json.dumps(data, default=str)) < 4096

    with isolated_tmp():
        upload_queue.reset_for_tests()
        asyncio.run(_main())
