"""repository.py - Shopping domain persistence: cart, orders, and the unified
chat history table (conversation_messages), used for both shopping and
operations turns so /api/history can restore the whole conversation.

Ported from npk_shopping_chatbot/app.py's inline DB helpers, adapted to use
the shared connection pool (database/connection.py, originally from the
operations bot) instead of shopping's own separate MySQLConnectionPool — both
domains now read/write the same database.
"""

import json
import decimal
import uuid
from datetime import datetime

from database.connection import DatabaseManager


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


def save_conversation_message(sid, conversation_id, role, message, products=None, cart_action=None):
    """Persist one chat turn (user or assistant), from EITHER domain, so
    /api/history can restore the full unified chat UI on page reload."""
    try:
        with DatabaseManager() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO conversation_messages
                       (session_id, conversation_id, role, message, products_json, cart_action_json)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    sid,
                    conversation_id,
                    role,
                    message,
                    json.dumps(products) if products else None,
                    json.dumps(cart_action) if cart_action else None,
                ),
            )
            conn.commit()
            cur.close()
    except Exception as e:
        # Logging is best-effort — never let a history-write failure break the
        # actual chat response the user is waiting on.
        print(f"[save_conversation_message] Error: {e}")


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


def get_last_shown_price_range(sid):
    prices = [float(p["price"]) for p in get_last_shown_products(sid) if p.get("price") is not None]
    if not prices:
        return None
    return min(prices), max(prices)


# ─── Cart ───────────────────────────────────────────────────────────────────
def get_cart_totals(sid: str):
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT product_id, sku, product_name, price, quantity, image_url, category
               FROM cart_items
               WHERE session_id=%s
               ORDER BY added_at ASC""",
            (sid,),
        )
        items = cur.fetchall()
        cur.close()

    for it in items:
        it["price"] = to_json_safe(it["price"])

    total = sum(float(it["price"]) * int(it["quantity"]) for it in items)
    return items, total


def add_to_cart(sid, product_id, product_name, price, quantity, sku, image_url, category):
    # cart_items has a FK to cart_sessions, so make sure the session row
    # exists first.
    ensure_cart_session(sid)

    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO cart_items
                   (session_id, product_id, sku, product_name, price, quantity, image_url, category)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE quantity = quantity + VALUES(quantity)""",
            (sid, product_id, sku, product_name, price, quantity, image_url, category),
        )
        conn.commit()
        cur.close()


def remove_from_cart(sid, product_id):
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM cart_items WHERE session_id=%s AND product_id=%s",
            (sid, product_id),
        )
        conn.commit()
        cur.close()


def clear_cart(sid):
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM cart_items WHERE session_id=%s", (sid,))
        conn.commit()
        cur.close()


# ─── Orders ───────────────────────────────────────────────────────────────────
def _generate_order_ref() -> str:
    return "ORD-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6].upper()


def place_order(sid, name, phone, address):
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT product_id, sku, product_name, price, quantity FROM cart_items WHERE session_id=%s",
            (sid,),
        )
        items = cur.fetchall()

        if not items:
            cur.close()
            return {"success": False, "error": "Your cart is empty"}

        total = sum(float(it["price"]) * int(it["quantity"]) for it in items)

        # order_ref has a UNIQUE constraint; a hex collision is astronomically
        # unlikely but retry a couple of times rather than erroring out on it.
        order_id = None
        order_ref = None
        for _ in range(3):
            order_ref = _generate_order_ref()
            try:
                cur.execute(
                    """INSERT INTO orders
                           (session_id, order_ref, total_amount, status,
                            customer_name, customer_phone, customer_address)
                       VALUES (%s, %s, %s, 'confirmed', %s, %s, %s)""",
                    (sid, order_ref, total, name, phone, address),
                )
                order_id = cur.lastrowid
                break
            except Exception:
                conn.rollback()
                continue

        if order_id is None:
            cur.close()
            return {"success": False, "error": "Could not place order, please try again"}

        for it in items:
            cur.execute(
                """INSERT INTO order_items (order_id, product_id, sku, product_name, price, quantity)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (order_id, it["product_id"], it["sku"], it["product_name"], it["price"], it["quantity"]),
            )

        cur.execute("DELETE FROM cart_items WHERE session_id=%s", (sid,))
        conn.commit()
        cur.close()

    return {
        "success":   True,
        "order_ref": order_ref,
        "message":   f"Order {order_ref} placed successfully!",
    }
