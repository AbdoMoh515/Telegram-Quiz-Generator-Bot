# utils.py
#
# Shared helpers for question parsing, formatting and dispatch.
#
# This module is intentionally importable WITHOUT aiogram installed so that
# the parser/formatter logic can be unit-tested offline. The aiogram imports
# below are optional and only required by send_telegram_quizzes() at runtime.

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

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
#: Telegram quiz-poll explanation (user-supplied clarification) limit.
#: Explanations longer than this are rejected at parse/validation time so
#: Telegram never silently truncates them.
MAX_POLL_EXPLANATION_LENGTH = 200

#: Safety caps for pasted/collected input.
MAX_INPUT_CHARS = 200_000
MAX_COLLECT_MESSAGES = 50
MAX_COLLECT_CHARS = 100_000

#: Max characters buffered for a single question block when streaming an
#: uploaded file from disk. A block (one numbered question) bigger than this
#: is skipped with a reason instead of being buffered indefinitely.
MAX_UPLOAD_BLOCK_CHARS = 32_768

#: How much of a spooled upload to read per disk chunk while streaming.
UPLOAD_READ_CHUNK_CHARS = 65_536

#: How long a parsed upload result stays on disk for Show-as-Text/export
#: after dispatch before age-based cleanup may remove it (hours).
UPLOAD_RESULT_TTL_SECONDS = 24 * 3600

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
#       "clarification": str,       # optional user-supplied clarification
#                                   # ("" when absent; never fabricated)
#   }

MCQ = "mcq"
WRITTEN = "written"


def make_mcq(question_num: str, question: str, options: List[str],
             correct_option_id: int, clarification: str = "") -> Dict[str, Any]:
    return {
        "type": MCQ,
        "question_num": str(question_num),
        "question": question,
        "options": list(options),
        "correct_option_id": correct_option_id,
        "answer_text": "",
        "clarification": str(clarification or ""),
    }


def make_written(question_num: str, question: str,
                 answer_text: str, clarification: str = "") -> Dict[str, Any]:
    return {
        "type": WRITTEN,
        "question_num": str(question_num),
        "question": question,
        "options": [],
        "correct_option_id": None,
        "answer_text": answer_text,
        "clarification": str(clarification or ""),
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
        clar = str(q.get("clarification") or "").strip()
        if clar and len(clar) > MAX_POLL_EXPLANATION_LENGTH:
            return (
                f"MCQ clarification too long for a Telegram quiz poll "
                f"explanation ({len(clar)} chars, "
                f"max {MAX_POLL_EXPLANATION_LENGTH})."
            )
    else:
        if not str(q.get("answer_text", "")).strip():
            return "Written question has an empty answer."
        if q.get("options"):
            return "Written question must not carry options."
        rendered = (
            f"<b>{disp}. "
            f"{html.escape(str(q.get('question', '')))}</b>\n\nAnswer: "
            f"<tg-spoiler>{html.escape(str(q.get('answer_text', '')))}</tg-spoiler>"
        )
        clar = str(q.get("clarification") or "").strip()
        if clar:
            rendered += (
                f"\nClarification: "
                f"<tg-spoiler>{html.escape(clar)}</tg-spoiler>"
            )
        rendered_len = len(rendered)
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
# number (``1.`` / ``1)``) and requires an ``Answer:`` line (an optional
# clarification line may follow it):
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
#   Optional user-supplied clarification AFTER the Answer line, either
#   labeled (``Clarification: ...`` / ``التوضيح: ...``, English label
#   case-insensitive, colon optional) or unlabeled free text. The Answer
#   itself stays exactly the single Answer line; clarification is stored
#   under ``clarification`` ("" when absent, never fabricated) and
#   round-trips via the canonical ``Clarification: ...`` label.
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
_ANSWER_RE = re.compile(r"^[ \t]*Answer[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_OPTION_LINE_RE = re.compile(r"^[ \t]*([A-Za-z])[.)]\s+(.*?)\s*$", re.MULTILINE)
_SINGLE_LETTER_RE = re.compile(r"^([A-Za-z])\s*[)]?\s*$")
# Optional user-supplied clarification label on the line(s) after Answer.
# English label is case-insensitive, Arabic as given; colon is optional.
_CLARIFICATION_LABEL_RE = re.compile(
    r"^[ \t]*(clarification|التوضيح)[ \t]*:?[ \t]*(.*)$",
    re.IGNORECASE,
)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_clarification(post_answer_text: str) -> str:
    """Extract optional user clarification from text after the Answer line.

    Accepts a labeled first line (``Clarification: ...`` /
    ``التوضيح: ...``, English case-insensitive, optional colon) or
    unlabeled free text. Returns "" when absent; never fabricates content.
    Multi-line content is preserved with newlines (each line collapsed).
    """
    s = (post_answer_text or "").strip()
    if not s:
        return ""
    lines = s.split("\n")
    label_match = _CLARIFICATION_LABEL_RE.match(lines[0])
    if label_match:
        first = _collapse(label_match.group(2))
        rest = [_collapse(ln) for ln in lines[1:]]
        # Drop leading/trailing blank continuation lines, keep inner ones.
        while rest and not rest[0]:
            rest.pop(0)
        while rest and not rest[-1]:
            rest.pop()
        parts = ([first] if first else []) + rest
        # If label line was empty and no continuation, there is no content.
        if not any(p for p in parts):
            return ""
        return "\n".join(parts).strip()
    # Unlabeled free text: collapse each line, trim blank edges.
    collapsed = [_collapse(ln) for ln in lines]
    while collapsed and not collapsed[0]:
        collapsed.pop(0)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    if not any(collapsed):
        return ""
    return "\n".join(collapsed).strip()


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
            question, skip = _parse_single_block(block, f"Block {i + 1}", seen_texts)
        except Exception as e:
            logger.error(
                f"Error processing block {i + 1}: {e}\nContent: {block[:200]}...",
                exc_info=True,
            )
            skipped.append({
                "number": f"Block {i + 1}",
                "reason": f"An unexpected error occurred: {e}",
            })
            continue
        if question is not None:
            questions.append(question)
        elif skip is not None:
            skipped.append(skip)

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


def _parse_single_block(
    block: str, label: str, seen_texts
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """Parse one stripped question *block*.

    Shared by the in-memory parser and the streaming upload parser so both
    accept exactly the same input. Returns ``(question, None)`` on success,
    ``(None, skipped_entry)`` when the block is rejected, and raises only on
    truly unexpected errors (the callers convert those to skipped entries).
    ``seen_texts`` only needs ``__contains__``/``add``: the in-memory
    parser passes a ``set``, the streaming parser a disk-backed
    :class:`_DiskSeenTexts`, with identical duplicate semantics.
    """
    header = _HEADER_RE.match(block)
    if not header:
        return None, {
            "number": label,
            "reason": "Could not find question number or text.",
        }
    question_num = header.group(1)

    answer_match = _ANSWER_RE.search(block)
    if not answer_match:
        return None, {
            "number": question_num,
            "reason": "No answer line found (expected 'Answer: ...').",
        }
    answer_raw = _collapse(answer_match.group(1))
    if not answer_raw:
        return None, {"number": question_num, "reason": "Empty answer text."}

    # Optional user-supplied clarification: line(s) AFTER the Answer
    # line in the same block. Labeled ("Clarification:"/"التوضيح:",
    # English case-insensitive, optional colon) or unlabeled free
    # text. The Answer itself stays exactly the single Answer line;
    # clarification is never mistaken for an option (only pre-answer
    # lines are scanned for options) nor for a next question (blocks
    # are split on numbering). Absent -> "" (never fabricated).
    clarification = _parse_clarification(block[answer_match.end():])

    # Option lines are only recognised BEFORE the Answer line, so a
    # written answer can never be mistaken for an option.
    pre_answer = block[: answer_match.start()]
    option_matches = _OPTION_LINE_RE.findall(pre_answer)

    if len(option_matches) >= 2:
        # ---------------- MCQ candidate ----------------
        letters = [m[0].lower() for m in option_matches]
        if len(set(letters)) != len(letters):
            return None, {
                "number": question_num,
                "reason": f"Duplicate option letters {letters}.",
            }
        options = [_collapse(m[1]) for m in option_matches]
        if any(not o for o in options):
            return None, {
                "number": question_num,
                "reason": "Found an empty option.",
            }
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
                return None, {
                    "number": question_num,
                    "reason": (
                        f"Answer {answer_raw!r} is not a single option "
                        f"letter, yet {len(option_matches)} options were found."
                    ),
                }
        if correct_letter not in letters:
            return None, {
                "number": question_num,
                "reason": (
                    f'Correct answer letter "{correct_letter}" '
                    f"not in options {letters}."
                ),
            }
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
            return None, {
                "number": question_num,
                "reason": "Empty question text.",
            }
        if question_text in seen_texts:
            return None, {
                "number": question_num,
                "reason": "Duplicate question.",
            }
        candidate = make_mcq(
            question_num, question_text, options,
            letters.index(correct_letter),
            clarification,
        )
        too_long = validate_question(candidate)
        if too_long is not None:
            return None, {
                "number": question_num,
                "reason": too_long,
            }
        seen_texts.add(question_text)
        return candidate, None
    elif len(option_matches) == 1:
        # ---------------- Ambiguous: neither clearly MCQ nor
        # written. Never guess: skip as incomplete.
        return None, {
            "number": question_num,
            "reason": (
                "Found only 1 option line: not a valid MCQ "
                "(needs 2+ options) nor a written question "
                "(needs no options)."
            ),
        }
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
            return None, {
                "number": question_num,
                "reason": "Empty question text.",
            }
        if question_text in seen_texts:
            return None, {
                "number": question_num,
                "reason": "Duplicate question.",
            }
        candidate = make_written(question_num, question_text, answer_raw,
                                 clarification)
        too_long = validate_question(candidate)
        if too_long is not None:
            return None, {
                "number": question_num,
                "reason": too_long,
            }
        seen_texts.add(question_text)
        return candidate, None


# ---------------------------------------------------------------------------
# Exports / previews (plain text and HTML)
# ---------------------------------------------------------------------------

def format_mcq_export(q: Dict[str, Any], number: int) -> str:
    lines = [f"{number}. {q['question']}"]
    for j, opt in enumerate(q["options"]):
        lines.append(f"{chr(97 + j)}) {opt}")
    lines.append(f"Answer: {chr(97 + q['correct_option_id'])}")
    clar = str(q.get("clarification") or "").strip()
    if clar:
        lines.append(f"Clarification: {clar}")
    return "\n".join(lines)


def format_written_export(q: Dict[str, Any], number: int) -> str:
    lines = [f"{number}. {q['question']}", f"Answer: {q['answer_text']}"]
    clar = str(q.get("clarification") or "").strip()
    if clar:
        lines.append(f"Clarification: {clar}")
    return "\n".join(lines)


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
    items are therefore skipped with a reason at parse time. An optional
    user-supplied clarification is shown on a new line under the answer in
    its own spoiler (answer stays spoiled too); the length check accounts
    for it.
    """
    question = html.escape(str(q["question"]))
    answer = html.escape(str(q["answer_text"]))
    text = f"<b>{number}. {question}</b>\n\nAnswer: <tg-spoiler>{answer}</tg-spoiler>"
    clar = str(q.get("clarification") or "").strip()
    if clar:
        text += f"\nClarification: <tg-spoiler>{html.escape(clar)}</tg-spoiler>"
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
    questions: List[Dict[str, Any]], skipped: List[Dict[str, str]],
    total_valid: Optional[int] = None, total_skipped: Optional[int] = None,
) -> str:
    """HTML preview: counts, representative questions, skipped reasons.

    Untrusted snippets are clipped as *raw* text and only then escaped, so
    the output never contains a half-cut HTML entity. The total is bounded
    by dropping whole skipped lines (never by cutting the final HTML), so
    tags stay balanced and the Send/Cancel instructions are always kept.

    *questions*/*skipped* may be bounded samples loaded from disk; pass the
    real totals via *total_valid*/*total_skipped* in that case (defaults
    fall back to ``len()`` for the in-memory path).
    """
    mcq_count = sum(1 for q in questions if q.get("type") == MCQ)
    written_count = sum(1 for q in questions if q.get("type") == WRITTEN)
    shown_valid = total_valid if total_valid is not None else len(questions)
    shown_skipped = total_skipped if total_skipped is not None else len(skipped)
    hidden_questions = max(0, shown_valid - len(questions))
    head = [
        "<b>🔍 Preview</b>",
        f"Valid: <b>{shown_valid}</b> "
        f"({mcq_count} multiple-choice, {written_count} written) | "
        f"Skipped: <b>{shown_skipped}</b>",
    ]
    if questions:
        head.append("")
        head.append("Samples:")
        for i, q in enumerate(questions[:PREVIEW_MAX_QUESTIONS_SHOWN], 1):
            head.append(html.escape(_preview_line(q, i)))
        if hidden_questions > 0 or len(questions) > PREVIEW_MAX_QUESTIONS_SHOWN:
            head.append(
                f"… and {max(hidden_questions, len(questions) - PREVIEW_MAX_QUESTIONS_SHOWN)} more."
            )
    foot = ["", "Press ✅ Send to dispatch, or ❌ Cancel to discard."]
    kept = list(skipped[:PREVIEW_MAX_SKIPPED_SHOWN])
    while True:
        hidden = shown_skipped - len(kept)
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


def build_empty_result_text(skipped: List[Dict[str, str]],
                             total_skipped: Optional[int] = None) -> str:
    """HTML notice for zero valid questions; same entity-safe bounding.

    *skipped* may be a bounded sample loaded from disk; pass the real total
    via *total_skipped* in that case (defaults to ``len()``).
    """
    shown_skipped = total_skipped if total_skipped is not None else len(skipped)
    head = ["❌ No valid questions could be extracted."]
    foot = [
        "",
        "Expected format: numbered questions ending with an "
        "<code>Answer:</code> line (options as <code>a)</code> lines for MCQ, "
        "no options for written).",
    ]
    kept = list(skipped[:PREVIEW_MAX_SKIPPED_SHOWN])
    while True:
        hidden = shown_skipped - len(kept)
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


async def _dispatch_single_question(
    bot: Bot, q: Dict[str, Any], chat_id: int, number: int
) -> None:
    """Send one parsed question; raises on validation/delivery failure."""
    reason = validate_question(q, number=number)
    if reason is not None:
        raise ValueError(reason)
    if q.get("type") == WRITTEN:
        await bot.send_message(
            chat_id=chat_id,
            text=format_written_send_text(q, number),
            parse_mode="HTML",
        )
    else:
        poll_kwargs: Dict[str, Any] = {
            "chat_id": chat_id,
            "question": format_mcq_poll_question(q, number),
            "options": format_mcq_poll_options(q),
            "type": "quiz",
            "correct_option_id": q["correct_option_id"],
            "is_anonymous": True,
        }
        # Optional user-supplied clarification -> quiz explanation.
        # Plain text, sent only when present (never fabricated).
        clar = str(q.get("clarification") or "").strip()
        if clar:
            poll_kwargs["explanation"] = clar
        await bot.send_poll(**poll_kwargs)
    await asyncio.sleep(0.5)


async def send_telegram_quizzes(
    bot: Bot, questions: List[Dict[str, Any]], chat_id: int, start_number: int
) -> Tuple[int, int, List[str], int]:
    """Dispatch questions: MCQ as anonymous quiz polls, written as spoiler text.

    *questions* may be any iterable (including a lazy generator over a
    disk store); items are sent one at a time in bounded memory.
    """
    sent_count = 0
    error_count = 0
    failed_questions: List[str] = []
    current_question_num = start_number

    for q in questions:
        try:
            await _dispatch_single_question(bot, q, chat_id, current_question_num)
            sent_count += 1
            current_question_num += 1
        except Exception as e:
            logger.error(f"Error sending quiz {q.get('question_num', '?')}: {e}")
            error_count += 1
            failed_questions.append(str(q.get("question_num", "?")))

    return sent_count, error_count, failed_questions, current_question_num


async def format_quiz_as_text(quiz: Poll, question_num: Optional[int] = None) -> str:
    """Convert a single forwarded Telegram quiz poll to text.

    This is the forwarded-poll export path (MCQ polls only) and is kept
    unchanged: a missing correct answer still yields "Answer: Not provided".
    When the source poll carries a non-empty ``explanation``, it is appended
    as a canonical ``Clarification: ...`` line; nothing is invented when the
    source has no explanation.
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

        explanation = str(getattr(quiz, "explanation", None) or "").strip()
        if explanation:
            text += f"\nClarification: {explanation}"

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
    base = os.environ.get("BOT_TEMP_DIR", "temp")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{prefix}{user_id}{suffix}")


# ---------------------------------------------------------------------------
# Disk-backed streaming parse for uploaded documents
# ---------------------------------------------------------------------------
#
# Uploaded files are never read wholly into RAM (no ``read()`` of the full
# file) and there is no per-file content-size cap: the file is streamed from
# disk in fixed-size chunks, split into numbered question blocks, and each
# block is parsed with the exact same single-block parser as pasted text.
# Accepted questions are appended to a JSONL store (one JSON object per
# line) and skipped blocks to a second JSONL store, so preview, dispatch
# and Show-as-Text all stream from disk in bounded memory. A single block
# bigger than MAX_UPLOAD_BLOCK_CHARS is skipped with a reason instead of
# being buffered indefinitely.

#: A line starting a new question block (same boundary as _BLOCK_SPLIT_RE).
_BLOCK_HEADER_LINE_RE = re.compile(r"^\s*(?:Q\s*)?\d+\s*[.\-)]")


def _stream_blocks_from_file(
    src_path: str, block_limit: int = MAX_UPLOAD_BLOCK_CHARS
) -> Iterator[Tuple[str, str]]:
    """Yield ``("block", text)`` / ``("oversized", label)`` from *src_path*.

    Reads the file in fixed-size chunks so peak memory stays bounded no
    matter how large the file is. Block boundaries are numbered header
    lines, matching the in-memory ``_BLOCK_SPLIT_RE`` semantics. Oversized
    blocks are discarded without ever being fully buffered; *label* carries
    the block's question number when its header was seen, else a Block N
    fallback.
    """
    pending = ""
    pieces: List[str] = []
    pieces_len = 0
    oversized = False
    oversized_label = ""
    block_index = 0
    finished = False

    def _header_number(line: str) -> Optional[str]:
        m = re.match(r"^\s*(?:Q\s*)?(\d+)\s*[.\-)]", line)
        return m.group(1) if m else None

    def _flush() -> Optional[Tuple[str, str]]:
        nonlocal pieces, pieces_len, oversized, oversized_label, block_index
        if oversized:
            label = oversized_label or f"Block {block_index + 1}"
            block_index += 1
            oversized = False
            oversized_label = ""
            pieces = []
            pieces_len = 0
            return ("oversized", label)
        text = "".join(pieces).strip()
        pieces = []
        pieces_len = 0
        if not text:
            return None
        block_index += 1
        return ("block", text)

    with open(src_path, "r", encoding="utf-8", errors="replace") as f:
        while True:
            chunk = f.read(UPLOAD_READ_CHUNK_CHARS)
            if chunk == "":
                finished = True
            pending += chunk.replace("\r\n", "\n").replace("\r", "\n")
            lines = pending.split("\n")
            # Last element is incomplete (no trailing newline yet) unless EOF.
            pending = "" if finished else lines.pop()
            if not finished and len(pending) > block_limit + UPLOAD_READ_CHUNK_CHARS:
                # A single line longer than the block budget: the current
                # block can never fit, and keeping the tail would grow memory
                # without bound. Doom the block (discarded until the next
                # header) and keep only a bounded tail slice.
                oversized = True
                pieces = []
                pieces_len = 0
                pending = pending[-4096:]
            out: List[Tuple[str, str]] = []
            for line in lines:
                if _BLOCK_HEADER_LINE_RE.match(line):
                    flushed = _flush()
                    if flushed is not None:
                        out.append(flushed)
                    num = _header_number(line)
                    if num is not None:
                        oversized_label = num
                if oversized:
                    continue
                pieces.append(line + "\n")
                pieces_len += len(line) + 1
                if pieces_len > block_limit:
                    # Block too big: drop what we buffered and discard
                    # lines until the next header (or EOF).
                    oversized = True
                    pieces = []
                    pieces_len = 0
            for item in out:
                yield item
            if finished:
                flushed = _flush()
                if flushed is not None:
                    yield flushed
                return


class _DiskSeenTexts:
    """SQLite-backed duplicate tracker for streaming upload parses.

    Implements the ``__contains__``/``add`` protocol used by
    :func:`_parse_single_block`, so duplicate semantics match the
    in-memory parser exactly -- but holds no question text in RAM.
    Membership is a UNIQUE-index lookup on the SHA-256 of the question
    text in a job-local SQLite file, so peak memory stays flat no matter
    how many questions the upload contains. Call :meth:`dispose` when
    the parse finishes; it closes the database and removes the file
    (long-term results are the JSONL stores, not this index).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (h TEXT PRIMARY KEY)")
        self._conn.commit()

    @staticmethod
    def _digest(item: str) -> str:
        return hashlib.sha256(item.encode("utf-8")).hexdigest()

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, str):
            return False
        cur = self._conn.execute(
            "SELECT 1 FROM seen WHERE h = ?", (self._digest(item),))
        return cur.fetchone() is not None

    def add(self, item: str) -> None:
        # No per-item commit: the single connection sees its own writes,
        # and dispose() commits once at the end of the parse.
        self._conn.execute(
            "INSERT OR IGNORE INTO seen (h) VALUES (?)",
            (self._digest(item),))

    def commit(self) -> None:
        self._conn.commit()

    def dispose(self) -> None:
        try:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
        except Exception:
            logger.warning("Could not close dedup database.", exc_info=True)
        finally:
            try:
                os.remove(self._db_path)
            except OSError:
                pass


def parse_upload_file_to_disk(
    src_path: str, job_dir: str,
    block_limit: int = MAX_UPLOAD_BLOCK_CHARS,
) -> Dict[str, Any]:
    """Stream-parse an uploaded file into JSONL stores under *job_dir*.

    Synchronous (run it in an executor from async code). Returns a summary
    dict ``{"valid_count", "skipped_count", "questions_file",
    "skipped_file"}``. Never holds more than one question block plus a
    fixed-size read chunk in RAM -- including duplicate tracking, which
    uses a job-local SQLite UNIQUE index on question-text hashes instead
    of an in-memory set, so peak memory stays flat no matter how many
    questions the upload contains. The dedup database is removed before
    returning (success or failure); long-term results are the two JSONL
    stores, cleaned up with the job directory.
    """
    os.makedirs(job_dir, exist_ok=True)
    questions_file = os.path.join(job_dir, "questions.jsonl")
    skipped_file = os.path.join(job_dir, "skipped.jsonl")
    valid_count = 0
    skipped_count = 0
    seen_texts = _DiskSeenTexts(os.path.join(job_dir, "dedup.db"))
    try:
        with open(questions_file, "w", encoding="utf-8") as qf, open(
            skipped_file, "w", encoding="utf-8"
        ) as sf:
            for kind, payload in _stream_blocks_from_file(src_path, block_limit):
                if kind == "oversized":
                    skipped_count += 1
                    sf.write(json.dumps(
                        {"number": payload,
                         "reason": (
                             "Question block exceeds the safe per-question memory "
                             f"limit ({block_limit} chars); skipped instead of "
                             "buffering it."),
                         }, ensure_ascii=False) + "\n")
                    continue
                try:
                    question, skip = _parse_single_block(
                        payload, f"Block {valid_count + skipped_count + 1}", seen_texts)
                except Exception as e:
                    logger.error(
                        f"Error processing streamed block: {e}\n"
                        f"Content: {payload[:200]}...", exc_info=True)
                    question, skip = None, {
                        "number": f"Block {valid_count + skipped_count + 1}",
                        "reason": f"An unexpected error occurred: {e}",
                    }
                if question is not None:
                    # Prefix-aware position check up front: the sequential
                    # dispatch number of this question is already known
                    # (1-based count of accepted questions so far + 1), so an
                    # MCQ that only overflows under its real prefix is skipped
                    # here instead of a later whole-list pass. This matches the
                    # in-memory fixpoint outcome: removals only shrink later
                    # numbers, so a single forward pass is exact.
                    pos = valid_count + 1
                    reason = None
                    if question.get("type") == MCQ:
                        reason = validate_question(question, number=pos)
                    if reason is not None:
                        skipped_count += 1
                        sf.write(json.dumps(
                            {"number": str(question.get("question_num", pos)),
                             "reason": reason}, ensure_ascii=False) + "\n")
                        continue
                    valid_count += 1
                    qf.write(json.dumps(question, ensure_ascii=False) + "\n")
                elif skip is not None:
                    skipped_count += 1
                    sf.write(json.dumps(skip, ensure_ascii=False) + "\n")
    finally:
        seen_texts.dispose()
    logger.info(
        f"Stream-parsed upload {src_path}: {valid_count} valid, "
        f"{skipped_count} skipped.")
    return {
        "valid_count": valid_count,
        "skipped_count": skipped_count,
        "questions_file": questions_file,
        "skipped_file": skipped_file,
    }


def iter_disk_questions(questions_file: str) -> Iterator[Dict[str, Any]]:
    """Lazily yield parsed questions from a JSONL store (bounded memory)."""
    with open(questions_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def iter_disk_skipped(skipped_file: str,
                      limit: Optional[int] = None) -> Iterator[Dict[str, str]]:
    """Lazily yield skipped entries from a JSONL store (bounded memory)."""
    count = 0
    with open(skipped_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if limit is not None and count >= limit:
                return
            count += 1
            yield json.loads(line)


def load_preview_sample(
    questions_file: str, skipped_file: str,
    max_questions: int = PREVIEW_MAX_QUESTIONS_SHOWN,
    max_skipped: int = PREVIEW_MAX_SKIPPED_SHOWN,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Load bounded preview samples from JSONL stores (never the full set)."""
    return (list(iter_disk_questions_bounded(questions_file, max_questions)),
            list(iter_disk_skipped(skipped_file, max_skipped)))


def iter_disk_questions_bounded(
    questions_file: str, limit: int
) -> Iterator[Dict[str, Any]]:
    """Yield at most *limit* questions from a JSONL store."""
    count = 0
    for q in iter_disk_questions(questions_file):
        if count >= limit:
            return
        count += 1
        yield q


def stream_questions_to_export_file(
    questions_file: str, export_path: str
) -> int:
    """Serialize a JSONL question store to the strict text format on disk.

    Streams in bounded memory; returns the number of questions written.
    """
    count = 0
    with open(export_path, "w", encoding="utf-8") as out:
        first = True
        for q in iter_disk_questions(questions_file):
            count += 1
            if not first:
                out.write("\n\n")
            first = False
            out.write(format_question_export(q, count))
    return count


def stream_questions_to_text_list(
    questions: Iterable[Dict[str, Any]],
) -> List[str]:
    """Serialize an in-memory question iterable to export strings."""
    return [format_question_export(q, i) for i, q in enumerate(questions, 1)]