"""memory.py - Settings for conversational context and long-term customer
memory (domains/shopping/conversation.py, services/memory_service.py)."""

import os
from dotenv import load_dotenv

load_dotenv()

# Model that reads the conversation + memory and decides each shopping turn.
# Needs to be good at multi-turn reasoning; gpt-3.5-turbo is not.
CONVERSATION_MODEL = os.getenv("SHOPPING_CONVERSATION_MODEL", "gpt-4.1-mini")

# Model that writes a search turn's reply once the results are known, in the
# customer's own language (domains/shopping/reply.py).
REPLY_MODEL = os.getenv("SHOPPING_REPLY_MODEL") or CONVERSATION_MODEL

# How many recent chat messages (user + assistant, all domains) the
# conversation model sees on every turn.
HISTORY_MESSAGES = int(os.getenv("SHOPPING_HISTORY_MESSAGES", "12"))

# Long-term memory on/off (facts + past-session summaries + welcome back).
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() == "true"

# Model that summarizes finished sessions and consolidates profile facts.
MEMORY_MODEL = os.getenv("MEMORY_MODEL", CONVERSATION_MODEL)

# A session counts as finished (and gets summarized) once it has been idle
# this long.
MEMORY_IDLE_MINUTES = int(os.getenv("MEMORY_IDLE_MINUTES", "30"))

# Past sessions older than this are not offered as "welcome back".
MEMORY_WELCOME_DAYS = int(os.getenv("MEMORY_WELCOME_DAYS", "30"))

# Past-session summaries included as context on every turn.
MEMORY_CONTEXT_SUMMARIES = int(os.getenv("MEMORY_CONTEXT_SUMMARIES", "3"))

# Max profile facts kept per customer (oldest dropped first).
MEMORY_MAX_FACTS = int(os.getenv("MEMORY_MAX_FACTS", "25"))

# Shared secret for verifying a logged-in customer's id sent by the host
# website ("<customer_id>.<hex HMAC-SHA256(secret, customer_id)>"). Empty =
# customer ids are ignored and memory is per device only. Never trust a raw,
# unsigned customer id: Magento ids are sequential, so anyone could read
# another customer's memory.
CUSTOMER_TOKEN_SECRET = os.getenv("CUSTOMER_TOKEN_SECRET", "")
