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

# How often the chat widget polls for agent replies while a handoff is active
# AND the realtime socket is not connected (fallback only).
POLL_INTERVAL_MS = int(os.getenv("HANDOFF_POLL_INTERVAL_MS", "3000"))

# ─── Realtime (Laravel Reverb, runs in naheedopsv2) ───────────────────────────
# Same app id/key/secret as naheedopsv2's REVERB_APP_* values. NiaBot uses them
# to publish customer messages to the agent and to authorize customers'
# private-handoff.{id} subscriptions. Leave REVERB_APP_KEY empty to disable
# realtime (the widget then polls).
REVERB_APP_ID = os.getenv("REVERB_APP_ID", "").strip()
REVERB_APP_KEY = os.getenv("REVERB_APP_KEY", "").strip()
REVERB_APP_SECRET = os.getenv("REVERB_APP_SECRET", "").strip()
# Where NiaBot's server reaches Reverb's HTTP API (publishing).
REVERB_HOST = os.getenv("REVERB_HOST", "").strip()
REVERB_PORT = int(os.getenv("REVERB_PORT", "443"))
REVERB_SCHEME = os.getenv("REVERB_SCHEME", "https").strip()
# Where customers' browsers open the websocket (public address, e.g. the
# nginx-proxied wss://ops.naheed.pk). Defaults to the values above.
REVERB_CLIENT_HOST = os.getenv("REVERB_CLIENT_HOST", REVERB_HOST).strip()
REVERB_CLIENT_PORT = int(os.getenv("REVERB_CLIENT_PORT", str(REVERB_PORT)))
REVERB_CLIENT_SCHEME = os.getenv("REVERB_CLIENT_SCHEME", REVERB_SCHEME).strip()
REVERB_TIMEOUT = float(os.getenv("REVERB_TIMEOUT", "3"))
