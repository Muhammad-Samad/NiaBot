"""logger.py - The one logging setup for the whole app.

setup_logging() configures the root logger once (console + logs/application.log,
level from LOG_LEVEL, default INFO), so every module's logger - whether it
comes from get_logger() or a plain logging.getLogger(__name__) - shares the
same handlers and format. Third-party HTTP/SDK loggers are kept at WARNING.

Levels used across the app:
  INFO    - startup/initialisation, which domain a message was routed to,
            key state changes (provider health, session closed, ...)
  WARNING - degraded but handled situations (fallbacks, rate limits, ...)
  ERROR   - failures (use logger.exception inside an except block)
  DEBUG   - diagnostics, off by default (LOG_LEVEL=DEBUG to see them)
"""

import logging
import os

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "openai", "chromadb", "urllib3", "mysql.connector")

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)

    os.makedirs("logs", exist_ok=True)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    log_file = logging.FileHandler("logs/application.log", encoding="utf-8")
    log_file.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(log_file)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(module_name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(module_name)
