"""memory_service.py - Long-term customer memory across chat sessions.

Three layers (see migrations/add_conversation_memory.sql):
  * short-term  – the last few messages + structured need of the CURRENT
                  session (domains/shopping: conversation_messages,
                  cart_sessions.conversation_state). Not handled here.
  * summaries   – chat_conversation_summaries: one rolling summary per past
                  session (sid), written once it has been idle for
                  MEMORY_IDLE_MINUTES. Drives "welcome back" and lets the
                  conversation model answer "what did I look at last time?".
  * facts       – chat_customer_memory.profile_json: durable facts about the
                  customer ("Has a son, about 25 years old", "prefers Nike"),
                  added live by the conversation model and consolidated
                  (deduped, conflicts resolved) whenever a session is
                  summarized.

Memory is keyed by a customer key, not the session id — "New Chat" mints a
new sid every time, so sid-keyed memory would be forgotten on every reset:
  * "cust:<id>" – a logged-in customer, only when the host site sends a
                  token signed with CUSTOMER_TOKEN_SECRET (raw ids are never
                  trusted: they're sequential, so they'd expose other
                  customers' memory).
  * "dev:<id>"  – a random per-browser device id from templates/index.html
                  that survives "New Chat".
Without either, there's no long-term memory (in-session context still works).

All memory work is best-effort: a failure here never breaks a chat reply.
"""

import hashlib
import hmac
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from ai.json_chat import chat_json
from config.memory import (
    CUSTOMER_TOKEN_SECRET, MEMORY_CONTEXT_SUMMARIES, MEMORY_ENABLED, MEMORY_IDLE_MINUTES,
    MEMORY_MAX_FACTS, MEMORY_MODEL, MEMORY_WELCOME_DAYS,
)
from config.shopping import openai_client
from database.connection import DatabaseManager
from utils.logger import get_logger

logger = get_logger(__name__)

_executor = ThreadPoolExecutor(max_workers=3)

# One lock per customer key, so a live fact write and a summary
# consolidation for the same customer can't overwrite each other.
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()

# Pending-summary checks run at most this often per customer (per process).
_SUMMARY_CHECK_INTERVAL_SECONDS = 600
_last_summary_check: dict[str, float] = {}

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,60}$")
_CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# Messages per session fed to the summarizer (newest kept).
_SUMMARY_MAX_MESSAGES = 60


def _lock_for(customer_key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(customer_key, threading.Lock())


# ─── Identity ──────────────────────────────────────────────────────────────────
def _verify_customer_token(token) -> str | None:
    """'<customer_id>.<hex HMAC-SHA256(CUSTOMER_TOKEN_SECRET, customer_id)>'
    -> customer_id, else None."""
    if not token or not CUSTOMER_TOKEN_SECRET:
        return None
    customer_id, _, signature = str(token).strip().partition(".")
    if not _CUSTOMER_ID_RE.match(customer_id) or not signature:
        return None
    expected = hmac.new(CUSTOMER_TOKEN_SECRET.encode(), customer_id.encode(), hashlib.sha256).hexdigest()
    return customer_id if hmac.compare_digest(expected, signature.lower()) else None


def resolve_customer_key(device_id=None, customer_token=None) -> str | None:
    if not MEMORY_ENABLED:
        return None
    customer_id = _verify_customer_token(customer_token)
    if customer_id:
        return f"cust:{customer_id}"
    if device_id and _DEVICE_ID_RE.match(str(device_id)):
        return f"dev:{device_id}"
    return None


def link_session(sid: str, customer_key: str | None):
    """Records which customer this session belongs to (cart_sessions row
    must already exist), then checks in the background whether any of the
    customer's earlier sessions are now finished and need summarizing."""
    if not customer_key:
        return
    try:
        with DatabaseManager() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE cart_sessions SET customer_key=%s
                   WHERE session_id=%s AND (customer_key IS NULL OR customer_key<>%s)""",
                (customer_key, sid, customer_key),
            )
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error(f"[memory] Failed to link session {sid}: {e}")
        return

    now = time.monotonic()
    if now - _last_summary_check.get(customer_key, 0) >= _SUMMARY_CHECK_INTERVAL_SECONDS:
        _last_summary_check[customer_key] = now
        _executor.submit(_safe, summarize_pending, customer_key)


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception as e:
        logger.exception(f"[memory] {fn.__name__} failed: {e}")


# ─── Profile facts ─────────────────────────────────────────────────────────────
def _load_profile(customer_key: str) -> dict:
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT profile_json FROM chat_customer_memory WHERE customer_key=%s", (customer_key,))
        row = cur.fetchone()
        cur.close()
    try:
        profile = json.loads(row["profile_json"]) if row and row["profile_json"] else {}
    except (TypeError, ValueError):
        profile = {}
    if not isinstance(profile, dict):
        profile = {}
    profile.setdefault("facts", [])
    return profile


def _save_profile(customer_key: str, profile: dict):
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO chat_customer_memory (customer_key, profile_json) VALUES (%s, %s) AS new
               ON DUPLICATE KEY UPDATE profile_json = new.profile_json""",
            (customer_key, json.dumps(profile, ensure_ascii=False)),
        )
        conn.commit()
        cur.close()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _merge_facts(existing: list[dict], texts: list[str]) -> list[dict]:
    """Keeps each fact's original 'noted' date when its text is unchanged;
    new facts are noted today. Oldest are dropped past MEMORY_MAX_FACTS."""
    by_norm = {_norm(f["text"]): f for f in existing if f.get("text")}
    today = date.today().isoformat()
    merged, seen = [], set()
    for text in texts:
        key = _norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(by_norm.get(key) or {"text": text, "noted": today})
    return merged[-MEMORY_MAX_FACTS:]


def remember_async(customer_key: str | None, facts: list[str]):
    if customer_key and facts:
        _executor.submit(_safe, _add_facts, customer_key, list(facts))


def _add_facts(customer_key: str, facts: list[str]):
    with _lock_for(customer_key):
        profile = _load_profile(customer_key)
        current = profile["facts"]
        profile["facts"] = _merge_facts(current, [f["text"] for f in current] + facts)
        _save_profile(customer_key, profile)
    logger.info(f"[memory] {customer_key}: remembered {facts}")


# ─── Context for the conversation model ────────────────────────────────────────
def get_context(customer_key: str | None) -> dict | None:
    """{"facts": [str], "past": [{"date", "summary"}]} or None."""
    if not customer_key:
        return None
    try:
        facts = [f"{f['text']} (noted {f.get('noted', '')[:7]})" for f in _load_profile(customer_key)["facts"]]
        with DatabaseManager() as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """SELECT summary, topics_json FROM chat_conversation_summaries
                   WHERE customer_key=%s ORDER BY last_message_id DESC LIMIT %s""",
                (customer_key, MEMORY_CONTEXT_SUMMARIES),
            )
            rows = cur.fetchall()
            cur.close()
        past = [{"date": _topics(r).get("last_active", ""), "summary": r["summary"]} for r in rows]
        return {"facts": facts, "past": past} if (facts or past) else None
    except Exception as e:
        logger.error(f"[memory] Failed to load context for {customer_key}: {e}")
        return None


def _topics(row) -> dict:
    try:
        data = json.loads(row["topics_json"]) if row.get("topics_json") else {}
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


# ─── Session summaries ─────────────────────────────────────────────────────────
_SUMMARY_PROMPT = f"""
You maintain long-term memory for NiaBot, the shopping and support assistant of Naheed.pk (Pakistani online store).
You get the customer's existing memory facts and the transcript of one finished chat session. Return ONLY JSON:
{{
  "summary": "2-3 sentences: what the customer wanted, what was shown, put in the cart or ordered, and anything left unresolved",
  "topics": ["short topic", ...],
  "resume_hint": "phrase that completes 'Last time you were looking at ___', e.g. 'gift ideas for your son', or null",
  "resume_prompt": "a first-person message the customer could send to continue, e.g. 'Show me gift ideas for my 25 year old son', or null",
  "facts": ["full updated list of durable customer facts"]
}}
- summary/topics in English, specific (product types, brands, recipients, budget). No greetings or filler.
  Always call the customer "the customer" (or "they") — never guess the customer's own gender, not even from
  words like "husband" or "wife".
- resume_hint/resume_prompt: only for an unfinished SHOPPING need worth continuing; null if the session was only
  greetings, policy questions, or support that was resolved.
- facts: merge the existing facts with new ones from this transcript into ONE clean list. Facts describe WHO the
  customer is and what they STABLY prefer — family members (relation, gender, age), sizes, brand/style/diet
  preferences, a usual budget. NOT what they browsed or asked in this session ("asked the price of X",
  "looking for outfits", "interested in wall decor") — that belongs in the summary.
  One fact per person/topic: merge duplicates ("Has a son, about 25" + "Son is 25 years old" -> one fact), keep
  the newer value on conflict (e.g. an updated age). Short and third-person ("Has a son, about 25 years old",
  "Husband (spouse is male)", "Prefers Nike shoes", "Shoe size 42"). Only what the customer stated or clearly
  implied. NEVER phone numbers, addresses, payment details, order numbers, or health, religious or political
  information. At most {MEMORY_MAX_FACTS}.
"""


def _pending_sessions(customer_key: str, limit: int) -> list[dict]:
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT cs.session_id, MAX(cm.id) AS last_id, DATE(MAX(cm.created_at)) AS last_active
               FROM cart_sessions cs
               JOIN conversation_messages cm ON cm.session_id = cs.session_id
               LEFT JOIN chat_conversation_summaries s ON s.session_id = cs.session_id
               WHERE cs.customer_key = %s
               GROUP BY cs.session_id, s.last_message_id
               HAVING MAX(cm.id) > COALESCE(s.last_message_id, 0)
                  AND MAX(cm.created_at) < NOW() - INTERVAL %s MINUTE
               ORDER BY last_id DESC
               LIMIT %s""",
            (customer_key, MEMORY_IDLE_MINUTES, limit),
        )
        rows = cur.fetchall()
        cur.close()
    return rows


def _optional_names(query: str, sid: str) -> list[str]:
    try:
        with DatabaseManager() as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(query, (sid,))
            names = [r["product_name"] for r in cur.fetchall()]
            cur.close()
        return names
    except Exception as e:
        logger.debug(f"[memory] Optional lookup skipped: {e}")
        return []


def _transcript(sid: str) -> str:
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT role, message, products_json FROM conversation_messages
               WHERE session_id=%s ORDER BY id DESC LIMIT %s""",
            (sid, _SUMMARY_MAX_MESSAGES),
        )
        messages = list(reversed(cur.fetchall()))
        cur.close()
    # Cart/order tables come from SQL_SETUP.sql and aren't present on every
    # install — the transcript is still worth summarizing without them.
    cart = _optional_names("SELECT product_name FROM cart_items WHERE session_id=%s", sid)
    ordered = _optional_names(
        """SELECT oi.product_name FROM orders o JOIN order_items oi ON oi.order_id = o.id
           WHERE o.session_id=%s""",
        sid,
    )

    lines = []
    for m in messages:
        text = " ".join(str(m["message"]).split())[:400]
        line = f"{'customer' if m['role'] == 'user' else 'assistant'}: {text}"
        try:
            products = json.loads(m["products_json"]) if m["products_json"] else []
            names = [p.get("product_name", "") for p in products if isinstance(p, dict)][:5]
        except (TypeError, ValueError):
            names = []
        if names:
            line += f"  [showed: {'; '.join(names)}]"
        lines.append(line)
    if cart:
        lines.append(f"(left in cart: {'; '.join(cart)})")
    if ordered:
        lines.append(f"(ordered via chat: {'; '.join(ordered)})")
    return "\n".join(lines)


def _summarize_session(customer_key: str, row: dict):
    transcript = _transcript(row["session_id"])
    if not transcript:
        return
    profile = _load_profile(customer_key)
    existing = profile["facts"]
    user = (
        "## Existing facts\n" + ("\n".join(f"- {f['text']}" for f in existing) or "(none)")
        + f"\n\n## Transcript (session date {row['last_active']})\n{transcript}"
    )
    result = chat_json(openai_client, MEMORY_MODEL, _SUMMARY_PROMPT, user, max_tokens=700, temperature=0)

    summary = " ".join(str(result.get("summary") or "").split()) or "Short chat with no shopping details."
    topics = {
        "topics": [str(t) for t in (result.get("topics") or [])][:8],
        "resume_hint": result.get("resume_hint") or None,
        "resume_prompt": result.get("resume_prompt") or None,
        "last_active": str(row["last_active"]),
    }
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO chat_conversation_summaries
                   (customer_key, session_id, summary, topics_json, last_message_id)
               VALUES (%s, %s, %s, %s, %s) AS new
               ON DUPLICATE KEY UPDATE customer_key = new.customer_key, summary = new.summary,
                   topics_json = new.topics_json, last_message_id = new.last_message_id""",
            (customer_key, row["session_id"], summary, json.dumps(topics, ensure_ascii=False), row["last_id"]),
        )
        conn.commit()
        cur.close()

    facts = [" ".join(str(f).split()) for f in (result.get("facts") or []) if str(f).strip()]
    if facts:
        with _lock_for(customer_key):
            # Re-read: a live fact may have been added while the LLM ran.
            profile = _load_profile(customer_key)
            added_meanwhile = [f["text"] for f in profile["facts"] if f not in existing]
            profile["facts"] = _merge_facts(profile["facts"], facts + added_meanwhile)
            _save_profile(customer_key, profile)
    logger.info(f"[memory] {customer_key}: summarized session {row['session_id']}")


def summarize_pending(customer_key: str, limit: int = 3):
    """Summarizes this customer's finished sessions that have no summary yet
    (or have new messages since the last one)."""
    with _lock_for(f"summary:{customer_key}"):
        for row in _pending_sessions(customer_key, limit):
            try:
                _summarize_session(customer_key, row)
            except Exception as e:
                logger.exception(f"[memory] Failed to summarize session {row['session_id']}: {e}")


# ─── Welcome back ──────────────────────────────────────────────────────────────
def welcome(customer_key: str | None) -> dict | None:
    """{"message": str, "resume_prompt": str|None} for a returning customer
    opening a fresh chat, else None. Summarizes the most recent finished
    session first, so a customer who just pressed "New Chat" is greeted
    with what they were doing."""
    if not customer_key:
        return None
    try:
        summarize_pending(customer_key, limit=2)
        with DatabaseManager() as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """SELECT topics_json FROM chat_conversation_summaries
                   WHERE customer_key=%s ORDER BY last_message_id DESC LIMIT 1""",
                (customer_key,),
            )
            row = cur.fetchone()
            cur.close()
    except Exception as e:
        logger.error(f"[memory] Welcome lookup failed for {customer_key}: {e}")
        return None
    if not row:
        return None

    topics = _topics(row)
    try:
        days = (date.today() - date.fromisoformat(topics.get("last_active", ""))).days
    except ValueError:
        days = None
    if days is None or days > MEMORY_WELCOME_DAYS:
        return None

    hint = topics.get("resume_hint")
    if hint:
        message = f"Welcome back! Last time you were looking at {hint}. Want to pick up where you left off?"
    else:
        message = "Welcome back! Good to see you again."
    return {"message": message, "resume_prompt": topics.get("resume_prompt") if hint else None}


# ─── Forget ────────────────────────────────────────────────────────────────────
def forget(customer_key: str | None) -> bool:
    """Deletes everything remembered about this customer (facts, summaries,
    per-session need state) and unlinks their sessions. Chat transcripts
    (conversation_messages) are left as they are."""
    if not customer_key:
        return False
    with _lock_for(customer_key):
        with DatabaseManager() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM chat_customer_memory WHERE customer_key=%s", (customer_key,))
            cur.execute("DELETE FROM chat_conversation_summaries WHERE customer_key=%s", (customer_key,))
            # Unlinking stops these sessions from being summarized again.
            cur.execute(
                "UPDATE cart_sessions SET conversation_state=NULL, customer_key=NULL WHERE customer_key=%s",
                (customer_key,),
            )
            conn.commit()
            cur.close()
    logger.info(f"[memory] {customer_key}: memory forgotten")
    return True
