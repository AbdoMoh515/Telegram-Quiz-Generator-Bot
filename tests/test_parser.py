"""Parser/formatting tests for utils.py (run offline, no aiogram needed)."""

import asyncio

from utils import (
    build_empty_result_text,
    build_preview_text,
    extract_questions_from_text,
    format_mcq_poll_options,
    format_mcq_poll_question,
    format_question_export,
    format_quiz_as_text,
    format_written_send_text,
    is_allowed_document,
    send_telegram_quizzes,
    validate_question,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    MAX_POLL_EXPLANATION_LENGTH,
    MAX_POLL_QUESTION_LENGTH,
    MAX_POLL_OPTION_LENGTH,
)

MCQ_TEXT = """1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c"""

WRITTEN_TEXT = """1. Who wrote the novel 1984?
Answer: George Orwell"""

MIXED_TEXT = MCQ_TEXT + "\n\n" + """2. Who wrote the novel 1984?
Answer: George Orwell"""


def test_mcq_basic():
    questions, skipped = extract_questions_from_text(MCQ_TEXT)
    assert skipped == []
    assert len(questions) == 1
    q = questions[0]
    assert q["type"] == "mcq"
    assert q["question"] == "What is the capital of Egypt?"
    assert q["options"] == ["Giza", "Alexandria", "Cairo"]
    assert q["correct_option_id"] == 2
    assert validate_question(q) is None


def test_mcq_answer_case_insensitive():
    text = MCQ_TEXT.replace("Answer: c", "Answer: C")
    questions, skipped = extract_questions_from_text(text)
    assert len(questions) == 1
    assert questions[0]["correct_option_id"] == 2


def test_mcq_forwarded_export_style_answer():
    text = MCQ_TEXT.replace("Answer: c", "Answer: c) Cairo")
    questions, skipped = extract_questions_from_text(text)
    assert skipped == []
    assert len(questions) == 1
    assert questions[0]["correct_option_id"] == 2


def test_written_basic():
    questions, skipped = extract_questions_from_text(WRITTEN_TEXT)
    assert skipped == []
    assert len(questions) == 1
    q = questions[0]
    assert q["type"] == "written"
    assert q["question"] == "Who wrote the novel 1984?"
    assert q["answer_text"] == "George Orwell"
    assert q["options"] == []
    assert validate_question(q) is None


def test_written_single_letter_answer_is_not_mcq():
    text = "1. Pick the odd one out.\nAnswer: b"
    questions, skipped = extract_questions_from_text(text)
    assert len(questions) == 1
    assert questions[0]["type"] == "written"
    assert questions[0]["answer_text"] == "b"


def test_mixed_mcq_and_written():
    questions, skipped = extract_questions_from_text(MIXED_TEXT)
    assert skipped == []
    assert [q["type"] for q in questions] == ["mcq", "written"]


def test_mcq_full_text_answer_is_skipped_not_written():
    text = MCQ_TEXT.replace("Answer: c", "Answer: Paris")
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert len(skipped) == 1
    assert "single option letter" in skipped[0]["reason"]


def test_mcq_unknown_letter_skipped():
    text = MCQ_TEXT.replace("Answer: c", "Answer: d")
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert "not in options" in skipped[0]["reason"]


def test_single_option_block_skipped_as_incomplete():
    text = "1. Incomplete?\na) Only one\nAnswer: a"
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert "only 1 option" in skipped[0]["reason"]


def test_missing_answer_skipped():
    text = "1. No answer here?\na) Yes\nb) No"
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert "No answer line" in skipped[0]["reason"]


def test_empty_answer_skipped():
    text = "1. Empty answer?\nAnswer:   "
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert "Empty answer" in skipped[0]["reason"]


def test_empty_answer_followed_by_labeled_clarification_skipped():
    text = "1. What?\nAnswer:\nClarification: extra context"
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert "Empty answer" in skipped[0]["reason"]


def test_empty_answer_followed_by_unlabeled_clarification_skipped():
    text = "1. What?\nAnswer:\nextra context"
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert "Empty answer" in skipped[0]["reason"]


def test_empty_mcq_answer_followed_by_clarification_skipped():
    text = ("1. Pick one?\na) Yes\nb) No\nAnswer:\n"
            "Clarification: extra context")
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert "Empty answer" in skipped[0]["reason"]


def test_valid_answer_does_not_consume_following_clarification():
    questions, skipped = extract_questions_from_text(
        "1. What?\nAnswer: Paris\nClarification: extra context")
    assert skipped == []
    assert questions[0]["answer_text"] == "Paris"
    assert questions[0]["clarification"] == "extra context"


def test_duplicate_question_skipped():
    text = WRITTEN_TEXT + "\n\n" + WRITTEN_TEXT.replace("1.", "2.", 1)
    questions, skipped = extract_questions_from_text(text)
    assert len(questions) == 1
    assert len(skipped) == 1
    assert "Duplicate" in skipped[0]["reason"]


def test_unnumbered_prose_yields_nothing_valid():
    questions, skipped = extract_questions_from_text(
        "Hello, this is just a chat message with no questions."
    )
    assert questions == []
    assert len(skipped) == 1


def test_validate_question_rejects_bad_model():
    assert validate_question({}) is not None
    assert validate_question({"type": "mcq"}) is not None
    assert (
        validate_question(
            {"type": "mcq", "question": "Q?", "options": ["a"],
             "correct_option_id": 0, "answer_text": ""}
        )
        is not None
    )


def test_format_question_export_round_trip():
    questions, _ = extract_questions_from_text(MIXED_TEXT)
    assert format_question_export(questions[0], 1) == MCQ_TEXT
    assert format_question_export(questions[1], 2) == WRITTEN_TEXT.replace("1.", "2.", 1)


def test_preview_shows_counts_samples_and_reasons():
    questions, skipped = extract_questions_from_text(
        MIXED_TEXT + "\n\n3. Broken block with no answer"
    )
    text = build_preview_text(questions, skipped)
    assert "2" in text  # valid count
    assert "multiple-choice" in text and "written" in text
    assert "Skipped" in text
    assert len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH


def test_preview_escapes_untrusted_html():
    questions, _ = extract_questions_from_text(
        "1. Is <b>this</b> escaped?\nAnswer: <i>yes</i>"
    )
    text = build_preview_text(questions, [])
    assert "<b>this</b>" not in text
    assert "&lt;b&gt;" in text


def test_written_send_text_uses_spoiler_and_escapes():
    questions, _ = extract_questions_from_text(
        "1. Evil <b>Q</b>?\nAnswer: <tg-spoiler>x</tg-spoiler>"
    )
    text = format_written_send_text(questions[0], 5)
    assert text.startswith("<b>5. Evil &lt;b&gt;Q&lt;/b&gt;?</b>")
    assert "<tg-spoiler>&lt;tg-spoiler&gt;x&lt;/tg-spoiler&gt;</tg-spoiler>" in text


def test_poll_formatters_do_not_silently_truncate():
    q = {"question": "Q" * 10, "options": ["opt-a", "opt-b"]}
    assert format_mcq_poll_question(q, 1) == "1. " + "Q" * 10
    assert format_mcq_poll_options(q) == ["opt-a", "opt-b"]


def test_overlong_mcq_question_skipped_with_reason():
    text = f"1. {'Q' * 300}?\na) Yes\nb) No\nAnswer: a"
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert len(skipped) == 1
    assert "quiz poll" in skipped[0]["reason"]
    assert validate_question(
        {"type": "mcq", "question": "Q" * 300, "options": ["Yes", "No"],
         "correct_option_id": 0, "answer_text": "", "question_num": "1"}
    ) is not None


def test_overlong_mcq_option_skipped_with_reason():
    text = f"1. Pick?\na) {'o' * 101}\nb) Fine\nAnswer: b"
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert len(skipped) == 1
    assert "option too long" in skipped[0]["reason"]


def test_overlong_written_skipped_with_reason():
    text = f"1. Huge?\nAnswer: {'A' * 5000}"
    questions, skipped = extract_questions_from_text(text)
    assert questions == []
    assert len(skipped) == 1
    assert "Telegram message" in skipped[0]["reason"]
    assert validate_question(
        {"type": "written", "question": "Huge?", "options": [],
         "correct_option_id": None, "answer_text": "A" * 5000,
         "question_num": "1"}
    ) is not None


def test_format_written_send_text_never_returns_malformed_html():
    import pytest

    q = {"question": "Huge?", "answer_text": "A" * 5000}
    try:
        text = format_written_send_text(q, 1)
    except ValueError as e:
        assert "too long" in str(e)
    else:
        assert text.endswith("</tg-spoiler>")
        assert len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH
        raise AssertionError("expected ValueError for oversized written message")
    # Fitting messages always close the spoiler.
    ok = format_written_send_text({"question": "Q?", "answer_text": "Yes"}, 2)
    assert ok.endswith("</tg-spoiler>")
    assert "<tg-spoiler>" in ok


def test_is_allowed_document():
    assert is_allowed_document("quiz.txt")
    assert is_allowed_document("quiz.MD")
    assert not is_allowed_document("quiz.pdf")
    assert not is_allowed_document("quiz")
    assert not is_allowed_document(None)


def test_empty_result_text_mentions_reasons():
    _, skipped = extract_questions_from_text("1. No answer here?\na) Yes\nb) No")
    text = build_empty_result_text(skipped)
    assert "No valid questions" in text
    assert "No answer line" in text


class FakeBot:
    def __init__(self, fail_poll=False):
        self.polls = []
        self.messages = []
        self.fail_poll = fail_poll

    async def send_poll(self, **kwargs):
        if self.fail_poll:
            raise RuntimeError("poll boom")
        self.polls.append(kwargs)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


def test_send_dispatches_mcq_poll_and_written_spoiler():
    questions, _ = extract_questions_from_text(MIXED_TEXT)
    bot = FakeBot()
    sent, failed, failed_qs, nxt = asyncio.run(
        send_telegram_quizzes(bot, questions, chat_id=7, start_number=1)
    )
    assert (sent, failed, failed_qs, nxt) == (2, 0, [], 3)
    assert bot.polls[0]["type"] == "quiz"
    assert bot.polls[0]["is_anonymous"] is True
    assert bot.polls[0]["correct_option_id"] == 2
    assert bot.polls[0]["question"].startswith("1. ")
    assert "<tg-spoiler>George Orwell</tg-spoiler>" in bot.messages[0]["text"]
    assert bot.messages[0]["parse_mode"] == "HTML"


def test_send_failure_counted():
    questions, _ = extract_questions_from_text(MCQ_TEXT)
    bot = FakeBot(fail_poll=True)
    sent, failed, failed_qs, nxt = asyncio.run(
        send_telegram_quizzes(bot, questions, chat_id=7, start_number=4)
    )
    assert sent == 0 and failed == 1 and failed_qs == ["1"] and nxt == 4


def test_mcq_prefix_boundary_exact_fit_and_overflow():
    # "1. " prefix is 3 chars: 252-char text renders to exactly 255 (fits),
    # 253-char text renders to 256 (must be skipped, never truncated).
    q_ok = {"type": "mcq", "question": "Q" * 252, "options": ["Yes", "No"],
            "correct_option_id": 0, "answer_text": "", "question_num": "1"}
    assert validate_question(q_ok, number=1) is None
    assert format_mcq_poll_question(q_ok, 1) == "1. " + "Q" * 252
    assert len(format_mcq_poll_question(q_ok, 1)) == 255
    q_over = {"type": "mcq", "question": "Q" * 253, "options": ["Yes", "No"],
              "correct_option_id": 0, "answer_text": "", "question_num": "1"}
    reason = validate_question(q_over, number=1)
    assert reason is not None and "prefix" in reason
    # End-to-end through the parser: 254-char question text overflows "1. ".
    src = f"1. {'Q' * 254}\na) Yes\nb) No\nAnswer: a"
    questions, skipped = extract_questions_from_text(src)
    assert questions == []
    assert len(skipped) == 1 and "prefix" in skipped[0]["reason"]
    # No silent truncation at send time either.
    assert format_mcq_poll_question(q_over, 1) == "1. " + "Q" * 253


def test_mcq_batch_position_prefix_overflow():
    # 252-char questions fit at positions 1-9 ("N. " = 3 chars -> 255)
    # but overflow at position 10 ("10. " = 4 chars -> 256).
    blocks = []
    for i in range(1, 11):
        blocks.append(f"{i}. {'Q' * 248}{i:04d}\na) Yes\nb) No\nAnswer: a")
    src = "\n\n".join(blocks)
    questions, skipped = extract_questions_from_text(src)
    assert len(questions) == 9
    assert len(skipped) == 1 and "prefix" in skipped[0]["reason"]
    # Every kept question is dispatchable at its own sequential number.
    for pos, q in enumerate(questions, 1):
        assert validate_question(q, number=pos) is None
        assert len(format_mcq_poll_question(q, pos)) <= MAX_POLL_QUESTION_LENGTH


def test_preview_with_giant_entity_reason_stays_valid_html():
    import html as _html
    import re as _re
    questions, _ = extract_questions_from_text(MCQ_TEXT)
    skipped = [{"number": "2", "reason": "<" * 6000}]
    text = build_preview_text(questions, skipped)
    assert len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH
    assert "Send" in text and "Cancel" in text
    assert "Valid:" in text and "Skipped:" in text
    assert "�" not in text
    # No half-cut entity at the end, and unescape round-trips.
    assert not _re.search(r"&[A-Za-z0-9#]*$", text)
    _html.unescape(text)
    # Full reason must NOT be embedded (it was clipped pre-escape).
    assert "&lt;" in text  # clipped remainder still escaped, not raw "<"
    assert "<" * 10 not in text


def test_empty_result_with_giant_mixed_reason_stays_valid_html():
    import html as _html
    import re as _re
    skipped = [{"number": "1", "reason": "<>&" * 2000}]
    text = build_empty_result_text(skipped)
    assert len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH
    assert "No valid questions" in text
    assert "Answer:" in text  # action/format instructions preserved
    assert "�" not in text
    assert not _re.search(r"&[A-Za-z0-9#]*$", text)
    _html.unescape(text)
    assert "<" * 10 not in text


# ---------------------------------------------------------------------------
# Optional user-supplied clarification (MCQ + written)
# ---------------------------------------------------------------------------

def test_mcq_clarification_english_label():
    text = MCQ_TEXT + "\nClarification: Cairo has been the capital for ages."
    questions, skipped = extract_questions_from_text(text)
    assert skipped == []
    assert questions[0]["clarification"] == "Cairo has been the capital for ages."
    # Answer line stays exactly the single letter.
    assert questions[0]["correct_option_id"] == 2


def test_mcq_clarification_label_case_insensitive_and_colon_optional():
    for label in ["Clarification: foo", "clarification: foo", "CLARIFICATION: foo",
                  "Clarification foo", "clarification   foo", "Clarification :  foo"]:
        questions, skipped = extract_questions_from_text(MCQ_TEXT + f"\n{label}")
        assert skipped == [], label
        assert questions[0]["clarification"] == "foo", label


def test_mcq_clarification_arabic_label():
    for label in ["التوضيح: شرح مختصر", "التوضيح شرح مختصر", "التوضيح :  شرح مختصر"]:
        questions, skipped = extract_questions_from_text(MCQ_TEXT + f"\n{label}")
        assert skipped == [], label
        assert questions[0]["clarification"] == "شرح مختصر", label


def test_mcq_clarification_unlabeled():
    questions, skipped = extract_questions_from_text(
        MCQ_TEXT + "\nCairo has been the capital for ages."
    )
    assert skipped == []
    assert questions[0]["clarification"] == "Cairo has been the capital for ages."


def test_written_clarification_label_variants_and_unlabeled():
    questions, _ = extract_questions_from_text(
        WRITTEN_TEXT + "\nClarification: His most famous novel."
    )
    assert questions[0]["answer_text"] == "George Orwell"
    assert questions[0]["clarification"] == "His most famous novel."
    questions, _ = extract_questions_from_text(
        WRITTEN_TEXT + "\nالتوضيح: أشهر رواياته."
    )
    assert questions[0]["clarification"] == "أشهر رواياته."
    questions, _ = extract_questions_from_text(
        WRITTEN_TEXT + "\nJust extra context, no label."
    )
    assert questions[0]["clarification"] == "Just extra context, no label."


def test_clarification_multiline_preserved():
    text = MCQ_TEXT + "\nClarification: first line\nsecond line"
    questions, _ = extract_questions_from_text(text)
    assert questions[0]["clarification"] == "first line\nsecond line"
    text_w = WRITTEN_TEXT + "\nfirst line\nsecond line"
    questions_w, _ = extract_questions_from_text(text_w)
    assert questions_w[0]["clarification"] == "first line\nsecond line"
    assert questions_w[0]["answer_text"] == "George Orwell"


def test_clarification_not_mistaken_for_option():
    # A clarification line that looks like an option must not create options
    # for a written question nor break an MCQ.
    questions, skipped = extract_questions_from_text(
        "1. Explain?\nAnswer: yes\na) looks like option but is context"
    )
    assert skipped == []
    assert questions[0]["type"] == "written"
    assert questions[0]["answer_text"] == "yes"
    assert questions[0]["clarification"] == "a) looks like option but is context"


def test_absent_clarification_is_empty_and_not_exported():
    questions, _ = extract_questions_from_text(MCQ_TEXT)
    assert questions[0].get("clarification", "") in ("", None)
    assert "Clarification" not in format_question_export(questions[0], 1)
    questions_w, _ = extract_questions_from_text(WRITTEN_TEXT)
    assert questions_w[0].get("clarification", "") in ("", None)
    assert "Clarification" not in format_question_export(questions_w[0], 1)


def test_export_roundtrip_with_clarification():
    src_mcq = MCQ_TEXT + "\nClarification: Because it is."
    questions, _ = extract_questions_from_text(src_mcq)
    exported = format_question_export(questions[0], 1)
    assert exported == src_mcq
    again, skipped = extract_questions_from_text(exported)
    assert skipped == []
    assert again[0]["clarification"] == "Because it is."
    assert again[0]["correct_option_id"] == 2
    src_w = WRITTEN_TEXT + "\nClarification: His most famous novel."
    qw, _ = extract_questions_from_text(src_w)
    assert format_question_export(qw[0], 1) == src_w


def test_mcq_clarification_sent_as_poll_explanation_only_when_present():
    questions, _ = extract_questions_from_text(
        MCQ_TEXT + "\nClarification: plain context"
    )
    bot = FakeBot()
    asyncio.run(send_telegram_quizzes(bot, questions, chat_id=7, start_number=1))
    assert bot.polls[0]["explanation"] == "plain context"
    # Absence must not send the field at all.
    plain, _ = extract_questions_from_text(MCQ_TEXT)
    bot2 = FakeBot()
    asyncio.run(send_telegram_quizzes(bot2, plain, chat_id=7, start_number=1))
    assert "explanation" not in bot2.polls[0]


def test_written_clarification_spoiler_and_escaping():
    questions, _ = extract_questions_from_text(
        "1. Evil <b>Q</b>?\nAnswer: <i>yes</i>\nClarification: <b>why</b> & more"
    )
    text = format_written_send_text(questions[0], 1)
    # Answer stays in its own spoiler, clarification in a second spoiler below.
    assert text.count("<tg-spoiler>") == 2
    assert "<tg-spoiler>&lt;i&gt;yes&lt;/i&gt;</tg-spoiler>" in text
    assert "\nClarification: <tg-spoiler>&lt;b&gt;why&lt;/b&gt; &amp; more</tg-spoiler>" in text
    assert text.endswith("</tg-spoiler>")
    # No-clarification rendering is unchanged (single spoiler).
    plain, _ = extract_questions_from_text(WRITTEN_TEXT)
    assert format_written_send_text(plain[0], 1).count("<tg-spoiler>") == 1


def test_mcq_clarification_length_cap_rejected():
    long_clar = "x" * (MAX_POLL_EXPLANATION_LENGTH + 1)
    questions, skipped = extract_questions_from_text(
        MCQ_TEXT + f"\nClarification: {long_clar}"
    )
    assert questions == []
    assert len(skipped) == 1
    assert "clarification" in skipped[0]["reason"].lower()
    assert validate_question(
        {"type": "mcq", "question": "Q?", "options": ["A", "B"],
         "correct_option_id": 0, "answer_text": "",
         "clarification": long_clar, "question_num": "1"}
    ) is not None
    ok = "x" * MAX_POLL_EXPLANATION_LENGTH
    assert validate_question(
        {"type": "mcq", "question": "Q?", "options": ["A", "B"],
         "correct_option_id": 0, "answer_text": "",
         "clarification": ok, "question_num": "1"}
    ) is None


def test_written_rendered_length_accounts_for_clarification():
    fit_answer = "A" * 100
    questions, skipped = extract_questions_from_text(
        f"1. Q?\nAnswer: {fit_answer}"
    )
    assert skipped == []
    assert validate_question(questions[0]) is None
    # Same question with a huge clarification must be rejected, not truncated.
    huge = "C" * 5000
    q = {"type": "written", "question": "Q?", "options": [],
         "correct_option_id": None, "answer_text": fit_answer,
         "clarification": huge, "question_num": "1"}
    assert validate_question(q) is not None
    try:
        format_written_send_text(q, 1)
    except ValueError as e:
        assert "too long" in str(e)
    else:
        raise AssertionError("expected ValueError for oversized clarification")


def test_forwarded_poll_export_appends_explanation_only_when_present():
    class _Opt:
        def __init__(self, text):
            self.text = text

    class _Poll:
        def __init__(self, explanation=None):
            self.question = "Capital?"
            self.options = [_Opt("Giza"), _Opt("Cairo")]
            self.correct_option_id = 1
            self.explanation = explanation

    with_expl = asyncio.run(format_quiz_as_text(_Poll("Because Cairo."), 1))
    assert "\nClarification: Because Cairo." in with_expl
    assert with_expl.startswith("1. Capital?")
    # Round-trips back through the parser.
    parsed, skipped = extract_questions_from_text(with_expl)
    assert skipped == []
    assert parsed[0]["clarification"] == "Because Cairo."
    without = asyncio.run(format_quiz_as_text(_Poll(None), 1))
    assert "Clarification" not in without
    assert "Answer: b) Cairo" in without
    empty = asyncio.run(format_quiz_as_text(_Poll("   "), 1))
    assert "Clarification" not in empty
