"""base.py - Flask-level settings shared by the whole app.

Loads the .env file once, at import time, so every other module (including
ones that read os.getenv directly, like ai/ and services/) can rely on the
environment already being populated regardless of import order.
"""

import os
from dotenv import load_dotenv

load_dotenv()

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "npk-combine-dev-secret-key")
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# ─── Assistant persona ──────────────────────────────────────────────────────
# The name customers see in the chat UI and in the bot's own replies.
# "Nia" = Naheed Intelligent Assistant.
BOT_NAME = "NiaBot"
BOT_FULL_NAME = "Naheed Intelligent Assistant"
