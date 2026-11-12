"""database.py - Single shared MySQL database configuration.

All domains read and write the SAME database, on the assumption that it
contains both NiaBot's own tables (all prefixed ncb_: ncb_bot_sessions,
ncb_conversation_messages, ncb_conversation_sessions, ncb_intent_logs,
ncb_audit_logs, memory tables - see SQL_SETUP.sql and migrations/) and the
Magento schema the operations flows query (sales_order and friends,
complaint tickets). This module owns the connection settings; database/connection.py
(the shared MySQLConnectionPool + DatabaseManager context manager, ported
from the operations bot) is the only thing that imports from here.
"""

import os
from dotenv import load_dotenv

load_dotenv()

required_vars = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
missing_vars = [var for var in required_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Pool configuration
DB_POOL_NAME = os.getenv("DB_POOL_NAME", "npk_combine_pool")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_CONNECTION_TIMEOUT = int(os.getenv("DB_CONNECTION_TIMEOUT", "10"))
DB_POOL_RESET_SESSION = os.getenv("DB_POOL_RESET_SESSION", "False").lower() in ("true", "1", "yes")
