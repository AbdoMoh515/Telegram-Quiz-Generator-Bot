# Code Documentation

This document provides a high-level overview of the Ishraqat Fajr Quiz Bot's architecture, its components, and the key functions within the codebase. It is intended to help developers understand the project structure and contribute to its development.

## Project Structure

The project is organized into the following key files:

- `main.py`: The main entry point of the bot. It initializes the bot, registers all the message and callback handlers, and starts the polling loop.
- `handlers.py`: Contains the core logic for handling user interactions, such as commands, file uploads, and button presses.
- `handlers_admin.py`: Contains the logic for admin-only commands, such as managing user access.
- `utils.py`: A collection of utility functions that support the main application logic, including text extraction, question parsing, and sending quizzes.
- `config.py`: Manages the bot's configuration, loading sensitive data and settings from environment variables.
- `db.py`: Handles all interactions with the MySQL database, including user management and access control.
- `filedb.py`: Provides an alternative, file-based database system using JSON files for user data and access control.
- `requirements.txt`: Lists all the Python packages required to run the bot.

## Key Components and Functions

### `main.py`

- **`main()`**: The primary function that starts the bot. It sets up the `Dispatcher`, registers all handlers, and begins polling for updates from Telegram.
- **Handler Registration**: This section of the file maps user actions (like sending a command or a file) to the appropriate handler function from `handlers.py` and `handlers_admin.py`.

### `handlers.py`

- **`start_command()`**: Handles the `/start` command, greeting the user and providing the main menu.
- **`handle_file()`**: Processes uploaded PDF and text files. It extracts the text and passes it to the quiz extraction logic.
- **`handle_text_message()`**: A versatile handler that processes both button presses from the main menu and raw text messages containing quiz questions.
- **`process_quiz_extraction()`**: The core function for processing quiz data. It takes raw text, extracts questions using `utils.extract_questions_from_text`, and sends them as quizzes.
- **`handle_quiz_message()`**: Manages the collection of forwarded quizzes, storing them temporarily until the user finishes the extraction process.

### `handlers_admin.py`

- **`AccessControlMiddleware`**: A middleware that checks if a user is authorized to use the bot before processing their message.
- **Access requests**: New users are approved only via the Approve/Reject buttons on the request message DM'd to admins when an unapproved user sends `/start` (the `_handle_access_decision` path in `handlers_admin.py`). There are no manual allow/remove commands or buttons.
- **Revocation**: Removing an approved user is a manual server-side edit of `allowed_users.json`, followed by a service restart (the bot caches allowed users at startup). The removed user's next `/start` files a fresh access request.

### `utils.py`

- **`extract_text_from_file()`**: A helper function that reads a file (PDF or text) and returns its content as a string.
- **`extract_questions_from_text()`**: The key function for parsing questions from raw text. It uses a series of regular expressions to identify and extract the question, options, and correct answer from a variety of formats.
- **`send_telegram_quizzes()`**: Takes a list of parsed questions and sends them to the user as a series of Telegram quizzes.
- **`format_quiz_as_text()`**: Converts a Telegram quiz object back into a formatted text string, which is useful for displaying extracted quizzes.

### `config.py`

This file is responsible for loading all necessary configurations from a `.env` file, including the `TELEGRAM_TOKEN`, `LOG_CHANNEL_ID`, and `ADMIN_IDS`. This keeps sensitive information separate from the main codebase.

### `db.py` and `filedb.py`

These files provide two options for data storage:

- **`db.py`**: Connects to a MySQL database to store user information and manage access permissions. It is the more robust and scalable option.
- **`filedb.py`**: Uses JSON files for storage. It is simpler to set up but less suitable for a production environment.

The bot can be configured to use either of these systems depending on the deployment environment.
