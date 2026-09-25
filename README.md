Telegram Quiz Master Bot
📖 Overview
The Telegram Quiz Master Bot is a powerful and efficient tool designed to automate two primary tasks related to Telegram quizzes:

Quiz Creation: It can take a formatted text file (.txt or .md) containing questions and answers and automatically generate Telegram quizzes from it. Multiple-choice questions are sent as anonymous quiz polls; written questions are sent as ordinary messages with the answer hidden in a spoiler.

Quiz Extraction: It can receive multiple forwarded Telegram quizzes and consolidate them into a single, neatly formatted text file.

The bot features a secure, admin-only panel for user management and is built on a modern, asynchronous architecture using aiogram 3.x to ensure high performance and stability.

✨ Features
Create Quizzes from File: Supports `.txt` and `.md` file uploads (no PDF support).

Collect Mode: Send questions over several messages, then finish for a preview.

Preview Before Sending: Every parsed batch shows counts, sample questions and skipped reasons with explicit Send/Cancel controls. Nothing is sent without confirmation.

Robust Question Parsing: Reliably extracts numbered multiple-choice questions (options plus `Answer: letter`) and numbered written questions (no options plus `Answer: text`) from structured text.

Extract from Forwards: Intelligently collects forwarded quizzes and exports them to a single text file.

Secure Admin Panel: Access is restricted to designated admin User IDs.

User access is managed in exactly two ways: allowing happens only via the ✅ Approve / ❌ Reject buttons on the access-request message DM'd to admins when an unapproved user sends `/start`; revoking happens only by editing `allowed_users.json` on the server (removing the user's entry) followed by a service restart, since the bot caches allowed users at startup.

Access Control: Only admins and specifically allowed users can interact with the bot's features.

Access Requests: When an unapproved user sends `/start`, every admin gets a DM with inline Approve/Reject buttons (escaped username when present, full name, numeric ID). Repeat `/start` never spams admins; races resolve to a single winner and all admin notifications are retired with the outcome. See "Access requests" below.

Upload Queue: All `.txt`/`.md` document uploads share one global FIFO queue with at most two concurrent workers; parsing streams from disk in bounded memory with no per-file content cap. See "Upload queue & resource model" below.

AI Prompt Helper: Provides users with a ready-to-use prompt to format both multiple-choice and written questions correctly using an AI assistant. The AI is instructed to reply with only one copy-ready fenced code block.

High Performance:

Uses an efficient "batch collector" for forwarded quizzes to handle large volumes without crashing.

Processes file I/O in a non-blocking way to keep the bot responsive at all times.

Stable & Modern: Built with aiogram 3.x and its Finite State Machine (FSM) for robust state management.

📂 Project Structure
The project is organized into several focused modules:

main.py: The main entry point for the bot. Initializes the dispatcher and registers all handlers.

handlers.py: Contains the core logic for user-facing features (start, help, file processing, quiz collection).

handlers_admin.py: Contains all logic for the admin panel, including the access control middleware and the Approve/Reject handling for access requests.

keyboards.py: Defines all the reply and inline keyboards used for the bot's interface.

utils.py: A collection of helper functions for tasks like text extraction, question parsing, and file saving.

states.py: Defines the formal states for the Finite State Machine (FSM).

filedb.py: A simple, thread-safe, file-based database system using JSON files to store user data.

config.py: Manages loading configuration and settings from the .env file.

requirements.txt: Lists all the necessary Python packages for the project.

.env: The local configuration file where you store your secrets (not included in version control).

🚀 Setup and Installation
Follow these steps to get your bot running locally.

1. Prerequisites
Python 3.10 or newer.

2. Installation Steps
Clone the Repository (or Download Files)

Download all the project files into a single directory.

Create and Activate a Virtual Environment

This is a crucial step to keep your project dependencies isolated.

Open a terminal in your project directory and run:

# Create the virtual environment
python -m venv venv

# Activate it (on Windows)
.\venv\Scripts\Activate

# Activate it (on macOS/Linux)
source venv/bin/activate

Install Dependencies

Install all the required packages using the requirements.txt file:

pip install -r requirements.txt

Configure Environment Variables

Copy `.env.example` to `.env` in the project root and replace the placeholder token and admin ID. Optional settings and their defaults are documented in the example file. Keep `.env` private.

▶️ How to Run
With your virtual environment activated and your .env file configured, start the bot with this simple command:

python main.py

📝 Question Format
Every question block starts with a number and requires an `Answer:` line; an optional clarification line may follow the `Answer:` line. Put exactly one blank line between blocks.

Multiple choice (two or more options, single-letter answer):

1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c

Written (no options, free-text answer):

2. Who wrote the novel 1984?
Answer: George Orwell

Optional user-supplied clarification (MCQ and written): add a separate
line AFTER the Answer line in the same block. Labeled form (canonical for
output) is `Clarification: ...`; `التوضيح: ...` (Arabic label, colon
optional) and unlabeled free text on the line(s) after Answer are also
accepted. The English label is case-insensitive. Only include it when you
have extra context; it is never invented by the bot or the AI prompt.
MCQ clarifications are sent as the Telegram quiz poll explanation (plain
text, max 200 chars); written clarifications appear under the answer in
their own spoiler. Example:

1. What is the capital of Egypt?
a) Giza
b) Alexandria
c) Cairo
Answer: c
Clarification: Cairo has been the capital since the Fatimid era.

📥 Upload queue & resource model
Every `.txt`/`.md` DOCUMENT upload -- from any user -- joins a single global FIFO queue, and at most TWO uploads download/parse at the same time. The message handler itself never downloads: it only enqueues small metadata (job id, user/chat ids, Telegram file id, file name) and tells you your queue position, so extra uploads wait fairly instead of downloading concurrently.

Tradeoffs and limits, stated plainly:

- No per-file content-size cap: files are streamed from disk in fixed-size chunks and split into numbered question blocks, so a gigabyte-sized file uses roughly the same RAM as a tiny one (one block plus one read chunk). Duplicate detection is disk-backed too (a job-local SQLite UNIQUE index on question-text hashes, removed after the parse), so RAM stays flat no matter how many questions an upload holds.
- One guard remains per single question, not per file: a block bigger than 32 KiB (`MAX_UPLOAD_BLOCK_CHARS` in `utils.py`) is skipped with a reason instead of being buffered indefinitely.
- "No cap" does not mean infinite capacity. Two hard platform limits still apply and produce clear errors instead of silent failure: Telegram's Bot API lets bots download files up to 20 MB (larger files are refused at download), and a full disk aborts processing with a disk-full notice. A parse failure (disk-full, decoding/read error) notifies the affected user, drops the partial spool, and the worker moves on to the next queued upload -- one bad file never stalls the FIFO.
- Parsed questions live on disk (`temp/uploads/jobs/<job-id>/questions.jsonl`, one JSON object per line) from parse through preview, dispatch and Show-as-Text; FSM state only ever holds small identifiers (job id, token, counts) -- there is deliberately no per-upload list in FSM, so repeated uploads (including failed and empty ones) cannot grow it. Preview samples, dispatch and the Show-as-Text export all stream from disk in bounded memory.
- Upload preview buttons are bound to their owner: ✅ Send / ❌ Cancel taps are checked against the job's user/chat before the token is claimed, so a foreign press is refused without consuming, dispatching, or cancelling anything. Confirm/cancel only hold the claim lock for the quick token check-and-claim; the 0.5s-per-question dispatch and all disk/network cleanup run outside it, so one large upload never blocks other users' Send/Cancel.
- Pasted text, Collect mode and forwarded polls keep their current in-memory behavior (including the existing paste/collect caps) -- uploads are the only path that changed.
- Disk I/O runs in worker threads (`asyncio.to_thread`), never blocking the event loop; downloads happen only inside worker slots. Age-based cleanup (24 h TTL) plus cleanup on failure/cancel keeps the spool from growing forever.
- Restart behavior: at startup the bot re-queues spooled uploads that never started (oldest first) and re-registers live previews, so preview buttons sent before a restart keep working. Results whose files are gone answer "expired, please re-upload" instead of failing obscurely. Override the spool location with the `BOT_TEMP_DIR` environment variable (default `temp/`, already git-ignored); tests always use isolated temp dirs.

🔐 Access requests (admin approvals)
- An unapproved user sending `/start` is recorded (username + full name refreshed on every `/start` via `upsert_user`, older fields preserved) and gets a "pending review" notice. Admins and already-approved users never trigger requests.
- Each id in `ADMIN_IDS` receives a DM showing the safely HTML-escaped Telegram username when present, the Telegram full name (first + last) and the numeric ID, with inline ✅ Approve / ❌ Reject buttons that act directly on the request.
- Anti-spam: repeat `/start` while a request is pending only reminds the user -- admins are not re-notified. A rejected user stays rejected on repeat `/start` (no new notifications).
- Races and stale taps: resolving is an atomic compare-and-set, so exactly one admin wins; losers and repeat tappers get "Already resolved", and taps for unknown requests get "no longer pending". Concurrent taps for the SAME user are additionally serialized on a per-user lock from resolve through button-retire, so a second admin tapping during the winner's allowed-list write waits instead of retiring live buttons mid-write; on write failure the request re-opens with buttons still live and the waiter retries, with exactly one user DM ever sent. Approving notifies the user, adds them to the allowed list, and edits every admin notification to show the outcome with buttons removed (best effort per message). Non-admin taps are refused.
- Transactional approve: if the allowed-list write fails after an admin wins the race, the request is sent back to pending (buttons stay live, the user is NOT notified) so a later Approve tap can retry -- at most one tap ever sends the user DM. Revoking an approved user is done by editing `allowed_users.json` on the server (removing the user's entry) followed by a service restart, since the bot caches allowed users at startup; the removed user's next `/start` files a fresh request instead of being welcomed on the stale approved record (`/start` never welcomes on the request record alone -- the allowed list is the source of truth).
- Requests live in a small stdlib-SQLite database under the ignored temp dir (`temp/access_requests.db`, 7-day pending TTL) -- never in the tracked `users.json` / `allowed_users.json`, which are only written by live admin allow/remove/approve actions. `/help` and `/myaccess` stay public; everything else stays behind the access gate.
