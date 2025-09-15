Telegram Quiz Master Bot
📖 Overview
The Telegram Quiz Master Bot is a powerful and efficient tool designed to automate two primary tasks related to Telegram quizzes:

Quiz Creation: It can take a formatted text file (.txt or .pdf) containing questions and answers and automatically generate anonymous Telegram quizzes from it.

Quiz Extraction: It can receive multiple forwarded Telegram quizzes and consolidate them into a single, neatly formatted text file.

The bot features a secure, admin-only panel for user management and is built on a modern, asynchronous architecture using aiogram 3.x to ensure high performance and stability.

✨ Features
Create Quizzes from File: Supports both .pdf and .txt file uploads.

Extract from Forwards: Intelligently collects forwarded quizzes and exports them to a single text file.

Robust Question Parsing: Reliably extracts questions, options, and answers from structured text.

Secure Admin Panel: Access is restricted to designated admin User IDs.

Interactive User Management: Admins can allow or remove users through a user-friendly, button-based interface, eliminating the need for manual commands.

Access Control: Only admins and specifically allowed users can interact with the bot's features.

AI Prompt Helper: Provides users with a ready-to-use prompt to format their questions correctly using an AI assistant.

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

Create a file named .env in the root of your project directory.

Copy the content from the example below and fill in your details.

.env file content:

# Get this token from Telegram's @BotFather
TELEGRAM_TOKEN="YOUR_TELEGRAM_BOT_TOKEN_HERE"

# A list of admin Telegram User IDs, separated by commas (no spaces)
# Get your ID from @userinfobot
ADMIN_IDS="123456789,987654321"

# (Optional) The ID of a channel where the bot can log errors
LOG_CHANNEL_ID="-1001234567890"

▶️ How to Run
With your virtual environment activated and your .env file configured, start the bot with this simple command:

python main.py
