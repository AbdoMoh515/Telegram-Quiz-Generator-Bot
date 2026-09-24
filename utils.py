# utils.py
#
# Shared helpers for question parsing, formatting and dispatch.
#
# This module is intentionally importable WITHOUT aiogram installed so that
# the parser/formatter logic can be unit-tested offline. The aiogram imports
# below are optional and only required by send_telegram_quizzes() at runtime.

import asyncio
import html
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:  # pragma: no cover - runtime-only dependency
    from aiogram import Bot
    from aiogram.types import Poll
except Exception:  # ImportError when running offline tests
    Bot = Any  # type: ignore
    Poll = Any  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Uploaded documents we accept. No OCR/PDF support: plain text only.
ALLOWED_DOCUMENT_EXTENSIONS = {".txt", ".md"}

#: Telegram hard limit for a single message.
MAX_TELEGRAM_MESSAGE_LENGTH = 4096

#: Telegram quiz-poll limits.
MAX_POLL_QUESTION_LENGTH = 255
MAX_POLL_OPTION_LENGTH = 100

#: Safety caps for pasted/collected input.
MAX_INPUT_CHARS = 200_000
MAX_COLLECT_MESSAGES = 50
MAX_COLLECT_CHARS = 100_000

#: How much of the preview to show.
PREVIEW_MAX_QUESTIONS_SHOWN = 3
PREVIEW_MAX_SKIPPED_SHOWN = 10

# ---------------------------------------------------------------------------
# Question model
# ---------------------------------------------------------------------------
#
# A parsed question is a plain dict so it stays JSON-serialisable for FSM
# storage:
#
#   {
#       "type": "mcq" | "written",
#       "question_num": "1",        # original number from the source text
#       "question": "...",          # question text, whitespace-collapsed
#       "options": [...],           # MCQ options ([] for written)
#       "correct_option_id": int,   # MCQ only (None for written)
#       "answer_text": str,         # written answer ("" for MCQ)
#   }

MCQ = "mcq"
WRITTEN = "written"


def make_mcq(question_num: str, question: str, options: List[str],
             correct_option_id: int) -> Dict[str, Any]:
    return {
        "type": MCQ,
        "question_num": str(question_num),
        "question": question,
        "options": list(options),
        "correct_option_id": correct_option_id,
        "answer_text": "",
    }


def make_written(question_num: str, question: str,
                 answer_text: str) -> Dict[str, Any]:
    return {
        "type": WRITTEN,
        "question_num": str(question_num),
        "question": question,
        "options": [],
        "correct_option_id": None,
        "answer_text": answer_text,
    }


def mcq_poll_rendered_length(question_text: str, number: Any) -> int:
    """Length of the poll question as actually sent (``"<n>. <text>"``)."""
    return len(f"{number}. {question_text}")


def validate_question(
    q: Dict[str, Any], number: Any = None
) -> Optional[str]:
    """Return None if *q* is valid, otherwise a human-readable reason.

    Besides structural checks this enforces Telegram's hard limits so that
    items accepted into the preview step are always dispatchable: MCQ poll
    questions/options must fit the poll caps, and a rendered written message
    must fit the 4096-char message cap. Oversized items are rejected (the
    parser turns this into a skipped entry) instead of being silently
    truncated, which would corrupt question/answer semantics.

    *number* is the sequential dispatch number used for the ``"N. "``
    prefix. Telegram counts the prefix in the 255-char poll-question limit,
    so ``len(question)`` alone is not enough. When *number* is None the
    question's own ``question_num`` is used as the prefix estimate.
    """
    if not isinstance(q, dict):
        return "Question is not a mapping."
    qtype = q.get("type")
    if qtype not in (MCQ, WRITTEN):
        return f"Unknown question type {qtype!r}."
    if not str(q.get("question", "")).strip():
        return "Empty question text."
    disp = number
    if disp is None:
        disp = q.get("question_num", 1)
    if disp is None or str(disp).strip() == "":
        disp = 1
    if qtype == MCQ:
        options = q.get("options") or []
        if len(options) < 2:
            return f"MCQ needs at least 2 options, found {len(options)}."
        idx = q.get("correct_option_id")
        if not isinstance(idx, int) or not 0 <= idx < len(options):
            return "MCQ correct_option_id is out of range."
        if any(not str(o).strip() for o in options):
            return "MCQ has an empty option."
        rendered_len = mcq_poll_rendered_length(str(q.get("question", "")), disp)
        if rendered_len > MAX_POLL_QUESTION_LENGTH:
            return (
                f"MCQ question too long for a Telegram quiz poll with "
                f"'{disp}. ' numbering prefix "
                f"({rendered_len} chars, "
                f"max {MAX_POLL_QUESTION_LENGTH})."
            )
        for o in options:
            if len(str(o)) > MAX_POLL_OPTION_LENGTH:
                return (
                    f"MCQ option too long for a Telegram quiz poll "
                    f"({len(str(o))} chars, max {MAX_POLL_OPTION_LENGTH}): "
                    f"{str(o)[:50]!r}."
                )
    else:
        if not str(q.get("answer_text", "")).strip():
            return "Written question has an empty answer."
        if q.get("options"):
            return "Written question must not carry options."
        rendered_len = len(
            f"<b>{disp}. "
            f"{html.escape(str(q.get('question', '')))}</b>\n\nAnswer: "
            f"<tg-spoiler>{html.escape(str(q.get('answer_text', '')))}</tg-spoiler>"
        )
        if rendered_len > MAX_TELEGRAM_MESSAGE_LENGTH:
            return (
                f"Written question too long for a Telegram message "
                f"({rendered_len} chars, max {MAX_TELEGRAM_MESSAGE_LENGTH})."
            )
    return None


def is_allowed_document(file_name: Optional[str]) -> bool:
    if not file_name:
        return False
    return os.path.splitext(file_name)[1].lower() in ALLOWED_DOCUMENT_EXTENSIONS


# ---------------------------------------------------------------------------
# File text extraction (plain text only)
# ---------------------------------------------------------------------------

def _blocking_text_extraction(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error extracting text from file: {e}", exc_info=True)
        return ""


async def extract_text_from_file(file_path: str) -> str:
    """Read a .txt/.md file without blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _blocking_text_extraction, file_path)


# ---------------------------------------------------------------------------
# Unified parser
# ---------------------------------------------------------------------------
#
# Strict, backward-compatible input. Every question block must start with a
# number (``1.`` / ``1)``) and end with an ``Answer:`` line:
#
#   Multiple choice (>= 2 options, single-letter answer):
#       1. What is ...?
#       a) First
#       b) Second
#       Answer: b
#
#   Written (no options, free-text answer):
#       2. Who wrote ...?
#       Answer: George Orwell
#
# Anti-confusion rules:
#   * A block WITH options is always an MCQ candidate. A non-letter answer
#     (e.g. ``Answer: Paris``) is skipped, never silently treated as written.
#   * A block WITHOUT options is always a written candidate, even if the
#     answer is a single letter (never mistaken for an MCQ).
#   * Blocks with exactly one option line are skipped as incomplete instead
#     of being guessed as either type.

_BLOCK_SPLIT_RE = re.compile(r"\n(?=\s*(?:Q\s*)?\d+\s*[.\-)])")
_HEADER_RE = re.compile(r"^\s*(?:Q\s*)?(\d+)\s*[.\-)]\s*(.*)$", re.DOTALL)
_ANSWER_RE = re.compile(r"^[ \t]*Answer\s*:\s*(.*?)\s*$", re.MULTILINE | re.DOTALL)
_OPTION_LINE_RE = re.compile(r"^[ \t]*([A-Za-z])[.)]\s+(.*?)\s*$", re.MULTILINE)
_SINGLE_LETTER_RE = re.compile(r"^([A-Za-z])\s*[)]?\s*$")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_questions_from_text(
    text: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Parse numbered MCQ and written questions from *text*.

    Returns ``(questions, skipped)`` where each skipped entry is
    ``{"number": ..., "reason": ...}``.
    """
    text = (text or "").replace("\r\n", "\n").strip()
    logger.info(f"Total length of extracted text: {len(text)} characters")

    questions: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    seen_texts = set()

    if not text:
        return questions, skipped

    blocks = _BLOCK_SPLIT_RE.split(text)
    logger.info(f"Found {len(blocks)} potential question blocks.")

    for i, raw_block in enumerate(blocks):
        block = raw_block.strip()
        if not block:
            continue
        try:
            header = _HEADER_RE.match(block)
            if not header:
                skipped.append({
                    "number": f"Block {i + 1}",
                    "reason": "Could not find question number or text.",
                })
                continue
            question_num = header.group(1)

            answer_match = _ANSWER_RE.search(block)
            if not answer_match:
                skipped.append({
                    "number": question_num,
                    "reason": "No answer line found (expected 'Answer: ...').",
                })
                continue
            answer_raw = _collapse(answer_match.group(1))
            if not answer_raw:
                skipped.append({"number": question_num, "reason": "Empty answer text."})
                continue

            # Option lines are only recognised BEFORE the Answer line, so a
            # written answer can never be mistaken for an option.
            pre_answer = block[: answer_match.start()]
            option_matches = _OPTION_LINE_RE.findall(pre_answer)

            if len(option_matches) >= 2:
                # ---------------- MCQ candidate ----------------
                letters = [m[0].lower() for m in option_matches]
                if len(set(letters)) != len(letters):
                    skipped.append({
                        "number": question_num,
                        "reason": f"Duplicate option letters {letters}.",
                    })
                    continue
                options = [_collapse(m[1]) for m in option_matches]
                if any(not o for o in options):
                    skipped.append({
                        "number": question_num,
                        "reason": "Found an empty option.",
                    })
                    continue
                letter_match = _SINGLE_LETTER_RE.match(answer_raw)
                correct_letter: Optional[str] = None
                if letter_match:
                    correct_letter = letter_match.group(1).lower()
                else:
                    # Tolerate the forwarded-export style "Answer: c) Cairo":
                    # a letter followed by ")" and the option text itself.
                    export_match = re.match(
                        r"^([A-Za-z])\s*[)]\s*(.+?)\s*$", answer_raw
                    )
                    if export_match:
                        letter = export_match.group(1).lower()
                        rest = _collapse(export_match.group(2))
                        if letter in letters and options[
                            letters.index(letter)
                        ].lower() == rest.lower():
                            correct_letter = letter
                    if correct_letter is None:
                        skipped.append({
                            "number": question_num,
                            "reason": (
                                f"Answer {answer_raw!r} is not a single option "
                                f"letter, yet {len(option_matches)} options were found."
                            ),
                        })
                        continue
                if correct_letter not in letters:
                    skipped.append({
                        "number": question_num,
                        "reason": (
                            f'Correct answer letter "{correct_letter}" '
                            f"not in options {letters}."
                        ),
                    })
                    continue
                first_option_pos = _OPTION_LINE_RE.search(pre_answer)
                assert first_option_pos is not None
                # Question text = everything between the "N." header and the
                # first option line, with the header marker stripped.
                head_line_end = block.find("\n")
                head_first_line = block if head_line_end == -1 else block[:head_line_end]
                head_text = re.sub(
                    r"^\s*(?:Q\s*)?\d+\s*[.\-)]\s*", "", head_first_line
                )
                middle = pre_answer[len(head_first_line):first_option_pos.start()]
                question_text = _collapse(head_text + " " + middle)
                if not question_text:
                    skipped.append({
                        "number": question_num,
                        "reason": "Empty question text.",
                    })
                    continue
                if question_text in seen_texts:
                    skipped.append({
                        "number": question_num,
                        "reason": "Duplicate question.",
                    })
                    continue
                candidate = make_mcq(
                    question_num, question_text, options,
                    letters.index(correct_letter),
                )
                too_long = validate_question(candidate)
                if too_long is not None:
                    skipped.append({
                        "number": question_num,
                        "reason": too_long,
                    })
                    continue
                questions.append(candidate)
                seen_texts.add(question_text)
            elif len(option_matches) == 1:
                # ---------------- Ambiguous: neither clearly MCQ nor
                # written. Never guess: skip as incomplete.
                skipped.append({
                    "number": question_num,
                    "reason": (
                        "Found only 1 option line: not a valid MCQ "
                        "(needs 2+ options) nor a written question "
                        "(needs no options)."
                    ),
                })
                continue
            else:
                # ---------------- Written candidate ----------------
                # No option lines at all, so the answer is free text --
                # even a single letter is a written answer here, never
                # mistaken for an MCQ.
                head_line_end = block.find("\n")
                head_first_line = block if head_line_end == -1 else block[:head_line_end]
                head_text = re.sub(
                    r"^\s*(?:Q\s*)?\d+\s*[.\-)]\s*", "", head_first_line
                )
                middle = pre_answer[len(head_first_line):]
                question_text = _collapse(head_text + " " + middle)
                if not question_text:
                    skipped.append({
                        "number": question_num,
                        "reason": "Empty question text.",
                    })
                    continue
                if question_text in seen_texts:
                    skipped.append({
                        "number": question_num,
                        "reason": "Duplicate question.",
                    })
                    continue
                candidate = make_written(question_num, question_text, answer_raw)
                too_long = validate_question(candidate)
                if too_long is not None:
                    skipped.append({
                        "number": question_num,
                        "reason": too_long,
                    })
                    continue
                questions.append(candidate)
                seen_texts.add(question_text)
        except Exception as e:
            logger.error(
                f"Error processing block {i + 1}: {e}\nContent: {block[:200]}...",
                exc_info=True,
            )
            skipped.append({
                "number": f"Block {i + 1}",
                "reason": f"An unexpected error occurred: {e}",
            })

    # Prefix-aware batch check: dispatch numbers questions sequentially from
    # ``start_number`` (the bot sends from 1), so an MCQ that fits under its
    # own ``question_num`` prefix may still overflow once its wider
    # sequential number (e.g. "100. ") is prepended. Re-check every accepted
    # MCQ with its actual 1-based position to a fixpoint (removals only
    # shrink later numbers, so this terminates exact) and skip overflow
    # instead of truncating.
    changed = True
    while changed:
        changed = False
        for pos, q in enumerate(list(questions), 1):
            if q.get("type") != MCQ:
                continue
            reason = validate_question(q, number=pos)
            if reason is not None:
                skipped.append({
                    "number": str(q.get("question_num", pos)),
                    "reason": reason,
                })
                questions.remove(q)
                changed = True
                break

    return questions, skipped


# ---------------------------------------------------------------------------
# Exports / previews (plain text and HTML)
# ---------------------------------------------------------------------------

def format_mcq_export(q: Dict[str, Any], number: int) -> str:
    lines = [f"{number}. {q['question']}"]
    for j, opt in enumerate(q["options"]):
        lines.append(f"{chr(97 + j)}) {opt}")
    lines.append(f"Answer: {chr(97 + q['correct_option_id'])}")
    return "\n".join(lines)


def format_written_export(q: Dict[str, Any], number: int) -> str:
    return f"{number}. {q['question']}\nAnswer: {q['answer_text']}"


def format_question_export(q: Dict[str, Any], number: int) -> str:
    """Serialize a parsed question back to the strict input format."""
    if q.get("type") == WRITTEN:
        return format_written_export(q, number)
    return format_mcq_export(q, number)


def _truncate(text: str, limit: int) -> str:
    """Clip *raw* (non-HTML) text to *limit* chars with an ellipsis."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _clip_raw(text: Any, limit: int) -> str:
    """Clip untrusted raw text BEFORE escaping (never cut an HTML entity)."""
    return _truncate(str(text or ""), limit)


def _joined_len(lines: List[str]) -> int:
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


#: Max raw chars kept per skipped reason in previews (clipped pre-escape).
PREVIEW_REASON_CLIP = 200
#: Max raw chars kept per skipped item number in previews.
PREVIEW_NUMBER_CLIP = 30


def format_mcq_poll_question(q: Dict[str, Any], number: int) -> str:
    """Plain-text quiz-poll question.

    Returned verbatim (no silent truncation): oversized questions are
    rejected by validate_question / the parser with a skip reason, so
    truncating here would only corrupt semantics.
    """
    return f"{number}. {q['question']}"


def format_mcq_poll_options(q: Dict[str, Any]) -> List[str]:
    """Poll options verbatim (no silent truncation; see above)."""
    return [str(o) for o in q["options"]]


def format_written_send_text(q: Dict[str, Any], number: int) -> str:
    """Ordinary message for a written question; answer hidden in a spoiler.

    Untrusted question/answer text is escaped with html.escape. There is no
    native Telegram "written poll", so no poll is faked here. The message is
    returned verbatim: if it exceeds Telegram's 4096-char cap a ValueError
    is raised instead of truncating the rendered HTML (truncating could drop
    the closing </tg-spoiler> or silently change the answer). Oversized
    items are therefore skipped with a reason at parse time.
    """
    question = html.escape(str(q["question"]))
    answer = html.escape(str(q["answer_text"]))
    text = f"<b>{number}. {question}</b>\n\nAnswer: <tg-spoiler>{answer}</tg-spoiler>"
    if len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
        raise ValueError(
            f"Written question too long for a Telegram message "
            f"({len(text)} chars, max {MAX_TELEGRAM_MESSAGE_LENGTH})."
        )
    return text


def _preview_line(q: Dict[str, Any], number: int) -> str:
    short = _truncate(str(q["question"]), 80)
    if q.get("type") == WRITTEN:
        return f"{number}. [written] {short}"
    letter = chr(97 + q["correct_option_id"])
    return (
        f"{number}. [mcq] {short} "
        f"({len(q['options'])} options, answer {letter})"
    )


def build_preview_text(
    questions: List[Dict[str, Any]], skipped: List[Dict[str, str]]
) -> str:
    """HTML preview: counts, representative questions, skipped reasons.

    Untrusted snippets are clipped as *raw* text and only then escaped, so
    the output never contains a half-cut HTML entity. The total is bounded
    by dropping whole skipped lines (never by cutting the final HTML), so
    tags stay balanced and the Send/Cancel instructions are always kept.
    """
    mcq_count = sum(1 for q in questions if q.get("type") == MCQ)
    written_count = sum(1 for q in questions if q.get("type") == WRITTEN)
    head = [
        "<b>🔍 Preview</b>",
        f"Valid: <b>{len(questions)}</b> "
        f"({mcq_count} multiple-choice, {written_count} written) | "
        f"Skipped: <b>{len(skipped)}</b>",
    ]
    if questions:
        head.append("")
        head.append("Samples:")
        for i, q in enumerate(questions[:PREVIEW_MAX_QUESTIONS_SHOWN], 1):
            head.append(html.escape(_preview_line(q, i)))
        if len(questions) > PREVIEW_MAX_QUESTIONS_SHOWN:
            head.append(
                f"… and {len(questions) - PREVIEW_MAX_QUESTIONS_SHOWN} more."
            )
    foot = ["", "Press ✅ Send to dispatch, or ❌ Cancel to discard."]
    kept = list(skipped[:PREVIEW_MAX_SKIPPED_SHOWN])
    while True:
        hidden = len(skipped) - len(kept)
        block: List[str] = []
        if skipped:
            block.append("")
            block.append("Skipped:")
            for s in kept:
                block.append(
                    f"• {html.escape(_clip_raw(s.get('number', '?'), PREVIEW_NUMBER_CLIP))}: "
                    f"{html.escape(_clip_raw(s.get('reason', ''), PREVIEW_REASON_CLIP))}"
                )
            if hidden > 0:
                block.append(f"… and {hidden} more.")
        lines = head + block + foot
        if _joined_len(lines) <= MAX_TELEGRAM_MESSAGE_LENGTH or not kept:
            return "\n".join(lines)
        kept.pop()


def build_empty_result_text(skipped: List[Dict[str, str]]) -> str:
    """HTML notice for zero valid questions; same entity-safe bounding."""
    head = ["❌ No valid questions could be extracted."]
    foot = [
        "",
        "Expected format: numbered questions ending with an "
        "<code>Answer:</code> line (options as <code>a)</code> lines for MCQ, "
        "no options for written).",
    ]
    kept = list(skipped[:PREVIEW_MAX_SKIPPED_SHOWN])
    while True:
        hidden = len(skipped) - len(kept)
        block: List[str] = []
        if kept:
            block.append("")
            block.append("Reasons:")
            for s in kept:
                block.append(
                    f"• {html.escape(_clip_raw(s.get('number', '?'), PREVIEW_NUMBER_CLIP))}: "
                    f"{html.escape(_clip_raw(s.get('reason', ''), PREVIEW_REASON_CLIP))}"
                )
            if hidden > 0:
                block.append(f"… and {hidden} more.")
        lines = head + block + foot
        if _joined_len(lines) <= MAX_TELEGRAM_MESSAGE_LENGTH or not kept:
            return "\n".join(lines)
        kept.pop()


async def send_telegram_quizzes(
    bot: Bot, questions: List[Dict[str, Any]], chat_id: int, start_number: int
) -> Tuple[int, int, List[str], int]:
    """Dispatch questions: MCQ as anonymous quiz polls, written as spoiler text."""
    sent_count = 0
    error_count = 0
    failed_questions: List[str] = []
    current_question_num = start_number

    for q in questions:
        try:
            reason = validate_question(q, number=current_question_num)
            if reason is not None:
                raise ValueError(reason)
            if q.get("type") == WRITTEN:
                await bot.send_message(
                    chat_id=chat_id,
                    text=format_written_send_text(q, current_question_num),
                    parse_mode="HTML",
                )
            else:
                await bot.send_poll(
                    chat_id=chat_id,
                    question=format_mcq_poll_question(q, current_question_num),
                    options=format_mcq_poll_options(q),
                    type="quiz",
                    correct_option_id=q["correct_option_id"],
                    is_anonymous=True,
                )
            sent_count += 1
            current_question_num += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending quiz {q.get('question_num', '?')}: {e}")
            error_count += 1
            failed_questions.append(str(q.get("question_num", "?")))

    return sent_count, error_count, failed_questions, current_question_num


async def format_quiz_as_text(quiz: Poll, question_num: Optional[int] = None) -> str:
    """Convert a single forwarded Telegram quiz poll to text.

    This is the forwarded-poll export path (MCQ polls only) and is kept
    unchanged: a missing correct answer still yields "Answer: Not provided".
    """
    try:
        prefix = f"{question_num}. " if question_num is not None else ""
        text = f"{prefix}{quiz.question}\n"

        correct_option_id = getattr(quiz, "correct_option_id", None)
        has_correct_answer = correct_option_id is not None

        for i, option in enumerate(quiz.options):
            option_text = option.text
            text += f"{chr(97 + i)}) {option_text}\n"

        if has_correct_answer:
            correct_letter = chr(97 + correct_option_id)
            correct_text = quiz.options[correct_option_id].text
            text += f"Answer: {correct_letter}) {correct_text}"
        else:
            text += "Answer: Not provided"

        return text

    except Exception as e:
        logger.error(f"Error formatting quiz: {e}", exc_info=True)
        return "Error formatting quiz"


def save_questions_to_file(questions: List[str], file_path: str) -> bool:
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(questions))
        return True
    except Exception as e:
        logger.error(f"Error saving questions to file: {e}", exc_info=True)
        return False


def get_temp_file_path(user_id: int, prefix: str = "quiz_", suffix: str = ".txt") -> str:
    os.makedirs("temp", exist_ok=True)
    return os.path.join("temp", f"{prefix}{user_id}{suffix}")