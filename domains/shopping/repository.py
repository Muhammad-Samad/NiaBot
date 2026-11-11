"""repository.py - Shopping domain persistence: per-chat session state
(cart_sessions - the name is historical) and the unified chat history table
(conversation_messages), used for both shopping and operations turns so
/api/history can restore the whole conversation.

There is no cart here: product cards add straight to the host website's
(Magento) cart through static/niabot-widget.js - see templates/index.html.

Ported from npk_shopping_chatbot/app.py's inline DB helpers, adapted to use
the shared connection pool (database/connection.py, originally from the
operations bot) instead of shopping's own separate MySQLConnectionPool — both
domains now read/write the same database.
"""

import json
import decimal
from datetime import datetime

from database.connection import DatabaseManager
from utils.logger import get_logger

logger = get_logger(__name__)


def to_json_safe(val):
    """Convert MySQL non-JSON-safe types."""
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    if isinstance(val, datetime):
        return val.isoformat()
    return val


# ─── Session / conversation bookkeeping ───────────────────────────────────────
def ensure_cart_session(sid, conversation_id=None):
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT IGNORE INTO cart_sessions (session_id, conversation_id) VALUES (%s, %s)",
            (sid, conversation_id),
        )
        if conversation_id:
            cur.execute(
                "UPDATE cart_sessions SET conversation_id=%s WHERE session_id=%s",
                (conversation_id, sid),
            )
        conn.commit()
        cur.close()


def save_conversation_message(sid, conversation_id, role, message, products=None, cart_action=None, domain=None):
    """Persist one chat turn (user or assistant), from ANY domain, so
    /api/history can restore the full unified chat UI on page reload.
    `domain` is the domain that handled the turn (shopping / operations /
    policy / menu)."""
    try:
        with DatabaseManager() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO conversation_messages
                       (session_id, conversation_id, role, message, products_json, cart_action_json, domain)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    sid,
                    conversation_id,
                    role,
                    message,
                    json.dumps(products) if products else None,
                    json.dumps(cart_action) if cart_action else None,
                    domain,
                ),
            )
            conn.commit()
            cur.close()
    except Exception as e:
        # Logging is best-effort — never let a history-write failure break the
        # actual chat response the user is waiting on.
        logger.error(f"Failed to save conversation message for session {sid}: {e}")


def session_owner(sid):
    """cart_sessions.customer_key for this sid ("cust:..." / "dev:..."), or None."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT customer_key FROM cart_sessions WHERE session_id=%s", (sid,))
        row = cur.fetchone()
        cur.close()
    return row["customer_key"] if row else None


def latest_session_for(customer_key):
    """The customer's session with the newest message (any device), or None."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT cs.session_id
               FROM cart_sessions cs
               JOIN conversation_messages cm ON cm.session_id = cs.session_id
               WHERE cs.customer_key = %s
               GROUP BY cs.session_id
               ORDER BY MAX(cm.id) DESC
               LIMIT 1""",
            (customer_key,),
        )
        row = cur.fetchone()
        cur.close()
    return row["session_id"] if row else None


def get_history(sid):
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT conversation_id FROM cart_sessions WHERE session_id=%s",
            (sid,),
        )
        row = cur.fetchone()
        conversation_id = row["conversation_id"] if row else None

        cur.execute(
            """SELECT role, message, products_json, cart_action_json
               FROM conversation_messages
               WHERE session_id=%s
               ORDER BY id ASC""",
            (sid,),
        )
        rows = cur.fetchall()
        cur.close()

    messages = []
    for r in rows:
        try:
            products = json.loads(r["products_json"]) if r["products_json"] else []
        except (TypeError, ValueError):
            products = []
        try:
            cart_action = json.loads(r["cart_action_json"]) if r["cart_action_json"] else None
        except (TypeError, ValueError):
            cart_action = None

        messages.append({
            "role":        r["role"],
            "message":     r["message"],
            "products":    products,
            "cart_action": cart_action,
        })

    return conversation_id, messages


# ─── Follow-up resolution helpers (shopping-specific) ─────────────────────────
def get_last_shown_products(sid):
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT products_json FROM conversation_messages
               WHERE session_id=%s AND role='assistant' AND products_json IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (sid,),
        )
        row = cur.fetchone()
        cur.close()

    if not row or not row.get("products_json"):
        return []
    try:
        return json.loads(row["products_json"])
    except (TypeError, ValueError):
        return []


def get_last_topic(sid):
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT last_topic FROM cart_sessions WHERE session_id=%s", (sid,))
        row = cur.fetchone()
        cur.close()
    return (row or {}).get("last_topic") or None


def set_last_topic(sid, topic):
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cart_sessions SET last_topic=%s WHERE session_id=%s",
            (topic[:512], sid),
        )
        conn.commit()
        cur.close()


def get_conversation_state(sid) -> dict:
    """Structured shopping state for this session (see
    domains/shopping/conversation.py). {} when none yet or unreadable."""
    try:
        with DatabaseManager() as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT conversation_state FROM cart_sessions WHERE session_id=%s", (sid,))
            row = cur.fetchone()
            cur.close()
        raw = (row or {}).get("conversation_state")
        state = json.loads(raw) if raw else {}
        return state if isinstance(state, dict) else {}
    except Exception as e:
        logger.error(f"Failed to read conversation state for session {sid}: {e}")
        return {}


def set_conversation_state(sid, state: dict):
    try:
        with DatabaseManager() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE cart_sessions SET conversation_state=%s WHERE session_id=%s",
                (json.dumps(state, ensure_ascii=False), sid),
            )
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error(f"Failed to save conversation state for session {sid}: {e}")


def get_recent_messages(sid, limit: int):
    """The last `limit` chat messages of this session, oldest first, from
    every domain. Assistant messages carry the names of the product cards
    they showed, so the conversation model knows what the customer saw."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            # age_minutes is computed on the DB clock, the same clock that
            # stamped created_at, so app-server timezone can't skew it.
            """SELECT id, role, message, products_json, domain, created_at,
                      TIMESTAMPDIFF(MINUTE, created_at, NOW()) AS age_minutes
               FROM conversation_messages
               WHERE session_id=%s
               ORDER BY id DESC LIMIT %s""",
            (sid, limit),
        )
        rows = cur.fetchall()
        cur.close()

    messages = []
    for r in reversed(rows):
        try:
            products = json.loads(r["products_json"]) if r["products_json"] else []
        except (TypeError, ValueError):
            products = []
        messages.append({
            "id":         r["id"],
            "role":       r["role"],
            "message":    r["message"],
            "products":   [p.get("product_name", "") for p in products if isinstance(p, dict)],
            "domain":      r["domain"],
            "created_at":  r["created_at"],
            "age_minutes": r["age_minutes"],
        })
    return messages


def get_recently_shown_product_ids(sid, turns: int = 3) -> set:
    """Product ids shown in the last `turns` assistant replies — excluded
    from a "show more" search so the customer sees new products."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT products_json FROM conversation_messages
               WHERE session_id=%s AND role='assistant' AND products_json IS NOT NULL
               ORDER BY id DESC LIMIT %s""",
            (sid, turns),
        )
        rows = cur.fetchall()
        cur.close()

    ids = set()
    for r in rows:
        try:
            ids.update(str(p.get("product_id")) for p in json.loads(r["products_json"]) if isinstance(p, dict))
        except (TypeError, ValueError):
            continue
    return ids


def get_last_shown_price_range(sid):
    prices = [float(p["price"]) for p in get_last_shown_products(sid) if p.get("price") is not None]
    if not prices:
        return None
    return min(prices), max(prices)
