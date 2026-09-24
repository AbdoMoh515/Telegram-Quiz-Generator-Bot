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

Interactive User Management: Admins can allow or remove users through a user-friendly, button-based interface, eliminating the need for manual commands.

Access Control: Only admins and specifically allowed users can interact with the bot's features.

AI Prompt Helper: Provides users with a ready-to-use prompt to format both multiple-choice and written questions correctly using an AI assistant. The AI is instructed to reply with only one copy-ready fenced code block.

High Performance:

Uses an efficient "batch collector" for forwarded quizzes to handle large volumes without crashing.

Processes file I/O in a non-blocking way to keep the bot responsive at all times.

Stable & Modern: Built with aiogram 3.x and its Finite State Machine (FSM) for robust state management.

📂 Project Structure
The project is organized into several focused modules:

main.py: The main entry point for the bot. Initializes the dispatcher and registers all handlers.

handlers.py: Contains the core logic for user-facing features (start, help, file processing, quiz collection).

handlers_admin.py: Contains all logic for the admin panel, including the access control middleware and interactive user management.

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
