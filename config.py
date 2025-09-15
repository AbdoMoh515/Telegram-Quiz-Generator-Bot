# config.py

import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Bot configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(',') if admin_id]

# Rate limiting
MIN_INTERVAL_BETWEEN_FILES = int(os.getenv("MIN_INTERVAL_BETWEEN_FILES", "60"))  # seconds

# Bot version
BOT_VERSION = "1.3-Refactored"

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# Validate configuration
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set in environment variables or .env file")
if not ADMIN_IDS:
    logger.warning("ADMIN_IDS are not set. The admin panel will not be available to anyone.")
if LOG_CHANNEL_ID == 0:
    logger.warning("LOG_CHANNEL_ID is not set. Error logging to Telegram channel will be disabled.")