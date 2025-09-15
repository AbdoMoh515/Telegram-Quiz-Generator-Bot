# utils.py

import logging
import fitz
import re
import asyncio
import os
from typing import List, Dict, Any, Tuple, Optional

from aiogram import Bot
from aiogram.types import Poll

logger = logging.getLogger(__name__)


def _blocking_pdf_extraction(pdf_path: str) -> str:
    """
    Synchronous function to extract text from a PDF.
    This should be run in a separate thread to avoid blocking asyncio.
    """
    text = ""
    try:
        with fitz.open(pdf_path) as doc:
            page_count = len(doc)
            if page_count == 0:
                logger.warning("PDF is empty: no pages found")
                return ""
            
            logger.info(f"Processing PDF with {page_count} pages")
            
            for page_num, page in enumerate(doc):
                try:
                    page_text = page.get_text("text")
                    page_text = re.sub(r' +', ' ', page_text)
                    page_text = re.sub(r'\n\s*\n', '\n\n', page_text)
                    text += page_text + "\n\n"
                except Exception as e:
                    logger.error(f"Error extracting text from page {page_num+1}: {str(e)}")
    except Exception as e:
        logger.error(f"Error opening PDF file: {str(e)}")
    
    return text


def _blocking_text_extraction(file_path: str) -> str:
    """
    Synchronous function to read a text file.
    This should be run in a separate thread to avoid blocking on large files.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error extracting text from file: {str(e)}", exc_info=True)
        return ""


async def extract_text_from_file(file_path: str) -> str:
    """
    Extract text from a file (PDF or text file) without blocking the bot.
    """
    loop = asyncio.get_running_loop()
    if file_path.lower().endswith('.pdf'):
        # Offload the blocking PDF processing to a separate thread
        return await loop.run_in_executor(None, _blocking_pdf_extraction, file_path)
    else:
        # Offload file reading to a separate thread
        return await loop.run_in_executor(None, _blocking_text_extraction, file_path)


def extract_questions_from_text(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Extract questions and answers from text using a robust, multi-stage approach."""
    # This function is CPU-bound but fast enough that it likely doesn't need to be threaded.
    # If it were slower, it could also be run with asyncio.to_thread.
    # The original implementation was already very good. No changes needed here.
    text = text.replace('\r\n', '\n').strip()
    logger.info(f"Total length of extracted text: {len(text)} characters")

    questions = []
    skipped_questions = []
    extracted_question_texts = set()

    question_blocks = re.split(r'\n(?=\s*(?:Q\s*)?\d+\s*[.\-)])', text)
    logger.info(f"Found {len(question_blocks)} potential question blocks.")

    for i, block in enumerate(question_blocks):
        block = block.strip()
        if not block:
            continue

        try:
            q_match = re.match(r'(?:Q\s*)?(\d+)\s*[.\-)]\s*(.*?)(?=\n\s*[a-zA-Z][.)])', block, re.DOTALL)
            if not q_match:
                skipped_questions.append({'number': f'Block {i+1}', 'reason': 'Could not find question number or text.'})
                continue

            question_num = q_match.group(1)
            question_text = q_match.group(2).strip().replace('\n', ' ')

            if not question_text:
                skipped_questions.append({'number': question_num, 'reason': 'Empty question text.'})
                continue
            if question_text in extracted_question_texts:
                skipped_questions.append({'number': question_num, 'reason': 'Duplicate question.'})
                continue

            answer_match = re.search(r'Answer\s*:\s*([a-zA-Z])', block, re.IGNORECASE)
            if not answer_match:
                skipped_questions.append({'number': question_num, 'reason': 'No answer line found.'})
                continue
            correct_letter = answer_match.group(1).lower()
            
            options_part_match = re.search(r'((?:\n\s*[a-zA-Z][.)].*?)+)(?=\n\s*Answer\s*:)', block, re.DOTALL)
            if not options_part_match:
                skipped_questions.append({'number': question_num, 'reason': 'No options found.'})
                continue
            
            options_text = options_part_match.group(1)
            option_matches = re.findall(r'\n\s*([a-zA-Z])[.)]\s*(.*?)(?=\n\s*[a-zA-Z][.)]|$)', options_text, re.DOTALL)

            if len(option_matches) < 2:
                skipped_questions.append({'number': question_num, 'reason': f'Found only {len(option_matches)} options.'})
                continue

            options = [opt[1].strip().replace('\n', ' ') for opt in option_matches]
            option_letters = [opt[0].lower() for opt in option_matches]
            
            try:
                correct_index = option_letters.index(correct_letter)
            except ValueError:
                skipped_questions.append({'number': question_num, 'reason': f'Correct answer letter "{correct_letter}" not in options {option_letters}.'})
                continue

            questions.append({
                'question_num': question_num,
                'question': question_text,
                'options': options,
                'correct_option_id': correct_index
            })
            extracted_question_texts.add(question_text)

        except Exception as e:
            logger.error(f"Error processing block {i+1}: {e}\nContent: {block[:200]}...", exc_info=True)
            skipped_questions.append({'number': f'Block {i+1}', 'reason': f'An unexpected error occurred: {e}'})

    return questions, skipped_questions

async def send_telegram_quizzes(bot: Bot, questions: List[Dict[str, Any]], chat_id: int, start_number: int) -> Tuple[int, int, List[str], int]:
    """Send questions as Telegram quizzes with sequential numbering."""
    sent_count = 0
    error_count = 0
    failed_questions = []
    current_question_num = start_number

    for q in questions:
        try:
            original_question = q['question']
            unnumbered_question = re.sub(r'^\d+\s*[.)]\s*', '', original_question)
            numbered_question = f"{current_question_num}. {unnumbered_question}"

            await bot.send_poll(
                chat_id=chat_id,
                question=numbered_question,
                options=q['options'],
                type='quiz',
                correct_option_id=q['correct_option_id'],
                is_anonymous=True,
            )
            sent_count += 1
            current_question_num += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending quiz {q.get('question_num', '?')}: {e}")
            error_count += 1
            failed_questions.append(q.get('question_num', '?'))

    return sent_count, error_count, failed_questions, current_question_num


# The rest of the functions in utils.py were well-written and don't need changes.
# format_quiz_as_text, save_questions_to_file, get_temp_file_path are all good.
# ... (keep your original format_quiz_as_text, save_questions_to_file, get_temp_file_path functions here)
async def format_quiz_as_text(quiz: Poll, question_num: Optional[int] = None) -> str:
    """
    Convert a single Telegram quiz to text format with clearly marked correct answer
    
    Args:
        quiz: Telegram Poll object
        question_num: Optional question number
        
    Returns:
        Formatted question text
    """
    try:
        prefix = f"{question_num}. " if question_num is not None else ""
        text = f"{prefix}{quiz.question}\n"
        
        correct_option_id = getattr(quiz, 'correct_option_id', None)
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
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(questions))
        return True
    except Exception as e:
        logger.error(f"Error saving questions to file: {e}", exc_info=True)
        return False

def get_temp_file_path(user_id: int, prefix: str = "quiz_", suffix: str = ".txt") -> str:
    os.makedirs("temp", exist_ok=True)
    return os.path.join("temp", f"{prefix}{user_id}{suffix}")