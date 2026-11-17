"""handoff.py - Human-agent handoff settings (domains/handoff).

NiaBot hands a conversation to a human CSR in naheedopsv2. Both apps share
the same MySQL database: NiaBot queues the handoff in
ncb_handoff_conversations and then asks naheedopsv2 to assign an agent
right away (signed server-to-server call). naheedopsv2 also assigns waiting
customers every minute, so a failed call only delays the assignment.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Master switch: false = NiaBot never hands over (old behaviour).
HANDOFF_ENABLED = os.getenv("HANDOFF_ENABLED", "true").strip().lower() == "true"

# naheedopsv2 base URL, e.g. https://ops.naheed.pk (no trailing slash).
OPS_BASE_URL = os.getenv("OPS_BASE_URL", "").strip().rstrip("/")

# Shared secret for signing calls to naheedopsv2 (/niabot/*).
# Must equal NIABOT_HANDOFF_SECRET in naheedopsv2's .env.
NIABOT_HANDOFF_SECRET = os.getenv("NIABOT_HANDOFF_SECRET", "").strip()

OPS_REQUEST_TIMEOUT = float(os.getenv("OPS_REQUEST_TIMEOUT", "4"))

# naheedopsv2's users table (its tables carry the nop_ prefix) - read for the
# agent's display name only.
OPS_USERS_TABLE = os.getenv("OPS_USERS_TABLE", "nop_users").strip()

# How often the chat widget polls for agent replies while a handoff is active.
POLL_INTERVAL_MS = int(os.getenv("HANDOFF_POLL_INTERVAL_MS", "3000"))
