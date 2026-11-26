"""repository.py - Handoff persistence.

Tables (created by naheedopsv2's Laravel migrations, shared DB):
  ncb_handoff_conversations - one row per handoff. NiaBot only INSERTs the
      'waiting' row and touches last_customer_message_at; naheedopsv2 owns
      every status change (assign -> open, close -> closed).
  ncb_conversation_messages - the unified transcript. Rows exchanged during a
      handoff carry handoff_id; agent replies are role='agent' (agent_id),
      status notes role='system'.

`active_key` is unique while a handoff is waiting/open (NULL once closed), so
a customer can only ever have one active handoff even if two requests race.
"""

from mysql.connector import errors as mysql_errors

from config.handoff import OPS_USERS_TABLE
from database.connection import DatabaseManager

_ACTIVE = ("waiting", "open")


def _active_key(sid: str, customer_id: int | None) -> str:
    return f"cust:{customer_id}" if customer_id else f"sid:{sid}"


def get_active(sid: str, customer_id: int | None) -> dict | None:
    """The customer's waiting/open handoff (any of their sessions), else the
    session's own one, else None."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        if customer_id:
            cur.execute(
                """SELECT * FROM ncb_handoff_conversations
                   WHERE status IN ('waiting', 'open') AND (customer_id = %s OR session_id = %s)
                   ORDER BY id DESC LIMIT 1""",
                (customer_id, sid),
            )
        else:
            cur.execute(
                """SELECT * FROM ncb_handoff_conversations
                   WHERE status IN ('waiting', 'open') AND session_id = %s
                   ORDER BY id DESC LIMIT 1""",
                (sid,),
            )
        row = cur.fetchone()
        cur.close()
    return row


def get_latest(sid: str, customer_id: int | None) -> dict | None:
    """Newest handoff of this customer/session in any status (the widget
    needs to see a just-closed one to leave human mode)."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        if customer_id:
            cur.execute(
                """SELECT * FROM ncb_handoff_conversations
                   WHERE customer_id = %s OR session_id = %s
                   ORDER BY id DESC LIMIT 1""",
                (customer_id, sid),
            )
        else:
            cur.execute(
                "SELECT * FROM ncb_handoff_conversations WHERE session_id = %s ORDER BY id DESC LIMIT 1",
                (sid,),
            )
        row = cur.fetchone()
        cur.close()
    return row


def get_by_id(handoff_id: int) -> dict | None:
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM ncb_handoff_conversations WHERE id = %s", (handoff_id,))
        row = cur.fetchone()
        cur.close()
    return row


def create(sid: str, ts_conversation_id: str | None, customer_id: int | None, customer_key: str | None,
           reason: str, reason_detail: str | None) -> tuple[dict, bool]:
    """Queues a handoff. Returns (row, created) - when the customer already
    has an active handoff that one is returned with created=False."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        # The AI conversation shown to the agent as context: everything in this
        # session up to now (the triggering turn is saved with handoff_id).
        cur.execute(
            "SELECT MAX(id) AS last_id FROM ncb_conversation_messages WHERE session_id = %s",
            (sid,),
        )
        last = cur.fetchone() or {}
        try:
            cur.execute(
                """INSERT INTO ncb_handoff_conversations
                       (session_id, ts_conversation_id, customer_id, customer_key, active_key,
                        status, reason, reason_detail, context_until_id, queued_at)
                   VALUES (%s, %s, %s, %s, %s, 'waiting', %s, %s, %s, NOW())""",
                (
                    sid,
                    ts_conversation_id,
                    customer_id,
                    customer_key,
                    _active_key(sid, customer_id),
                    reason,
                    (reason_detail or "")[:2000] or None,
                    last.get("last_id"),
                ),
            )
            handoff_id = cur.lastrowid
            conn.commit()
            created = True
        except mysql_errors.IntegrityError:
            # Duplicate active_key: another request queued it first.
            conn.rollback()
            handoff_id = None
            created = False
        cur.close()

    row = get_by_id(handoff_id) if handoff_id else get_active(sid, customer_id)
    return row, created


def touch_customer_message(handoff_id: int):
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE ncb_handoff_conversations SET last_customer_message_at = NOW() WHERE id = %s",
            (handoff_id,),
        )
        conn.commit()
        cur.close()


def mark_customer_read(handoff_id: int, last_id: int) -> bool:
    """Moves customer_last_read_id forward to last_id - never backwards and
    never past the handoff's newest message. True when it moved."""
    with DatabaseManager() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE ncb_handoff_conversations h
               JOIN (SELECT MAX(id) AS newest FROM ncb_conversation_messages WHERE handoff_id = %s) m
               SET h.customer_last_read_id = LEAST(%s, m.newest)
               WHERE h.id = %s AND COALESCE(h.customer_last_read_id, 0) < LEAST(%s, m.newest)""",
            (handoff_id, last_id, handoff_id, last_id),
        )
        moved = cur.rowcount > 0
        conn.commit()
        cur.close()
    return moved


def queue_position(handoff: dict) -> int | None:
    """1-based position in the waiting queue (oldest first), None when not waiting."""
    if not handoff or handoff.get("status") != "waiting":
        return None
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT COUNT(*) AS ahead FROM ncb_handoff_conversations
               WHERE status = 'waiting'
                 AND (queued_at < %s OR (queued_at = %s AND id < %s))""",
            (handoff["queued_at"], handoff["queued_at"], handoff["id"]),
        )
        row = cur.fetchone() or {}
        cur.close()
    return int(row.get("ahead") or 0) + 1


def agent_name(agent_id: int | None) -> str | None:
    if not agent_id:
        return None
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT first_name, name FROM {OPS_USERS_TABLE} WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return (row.get("first_name") or "").strip() or row.get("name")


def support_messages_after(handoff_id: int, after_id: int) -> list[dict]:
    """Agent replies and system notes of a handoff newer than after_id."""
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            f"""SELECT m.id, m.role, m.message, m.created_at,
                       u.first_name AS agent_first_name, u.name AS agent_login
                FROM ncb_conversation_messages m
                LEFT JOIN {OPS_USERS_TABLE} u ON u.id = m.agent_id
                WHERE m.handoff_id = %s AND m.id > %s AND m.role IN ('agent', 'system')
                ORDER BY m.id ASC""",
            (handoff_id, after_id),
        )
        rows = cur.fetchall()
        cur.close()
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "message": r["message"],
            "agent_name": ((r.get("agent_first_name") or "").strip() or r.get("agent_login")) if r["role"] == "agent" else None,
        }
        for r in rows
    ]


def agent_names(agent_ids: set[int]) -> dict[int, str]:
    ids = [int(a) for a in agent_ids if a]
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    with DatabaseManager() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT id, first_name, name FROM {OPS_USERS_TABLE} WHERE id IN ({placeholders})", ids)
        rows = cur.fetchall()
        cur.close()
    return {r["id"]: (r.get("first_name") or "").strip() or r.get("name") for r in rows}
