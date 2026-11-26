"""service.py - Human-agent handoff flow.

  start()            - queue a handoff (after the detector fired or the AI
                       crashed), tell the customer, ask naheedopsv2 to assign
  find_active()      - the customer's waiting/open handoff, if any
  customer_message() - while a handoff is active the AI is paused: the
                       message is stored for the agent, no AI reply
  updates()          - agent replies / status for the widget (fetched when a
                       Reverb event arrives, or by fallback polling)

All handoff messages go into ncb_conversation_messages (with handoff_id), so
/api/history shows one continuous chat: NiaBot, the agent, then NiaBot again
once the agent closes the conversation in naheedopsv2.
"""

import hashlib
import hmac
import threading
import time

import requests

from config.handoff import (
    HANDOFF_ENABLED,
    NIABOT_HANDOFF_SECRET,
    OPS_BASE_URL,
    OPS_REQUEST_TIMEOUT,
    POLL_INTERVAL_MS,
)
from domains.handoff import detector, realtime, repository
from domains.shopping import repository as chat_repository
from utils.logger import get_logger

logger = get_logger(__name__)

DOMAIN = "handoff"

TRANSFER_TEXT = (
    "I'm transferring you to our Naheed Customer Support team now. "
    "The agent will see our conversation so far, so you won't need to repeat yourself."
)
QUEUE_TEXT = (
    "All our agents are helping other customers at the moment - you are number {position} in the queue. "
    "Please stay in this chat; an agent will reply here as soon as possible."
)
CONNECTING_TEXT = "An agent will reply to you here in a moment."


def enabled() -> bool:
    return HANDOFF_ENABLED


def detect(result: dict) -> tuple[str, str] | None:
    if not HANDOFF_ENABLED:
        return None
    return detector.detect(result)


def find_active(sid: str, customer_id: int | None) -> dict | None:
    """Never raises: a handoff-table problem must not take the AI chat down."""
    if not HANDOFF_ENABLED:
        return None
    try:
        return repository.get_active(sid, customer_id)
    except Exception as e:
        logger.error(f"Handoff lookup failed, continuing with NiaBot (sid={sid}): {e}")
        return None


def start(sid: str, conversation_id: str, customer_key: str | None, customer_id: int | None,
          user_msg: str, reason: str, detail: str | None, ai_answer: str | None) -> dict:
    """Queues the handoff and returns the /api/chat response. Raises when the
    handoff can't be stored - the caller then falls back to NiaBot's reply."""
    handoff, created = repository.create(sid, conversation_id, customer_id, customer_key, reason, detail)
    if not handoff:
        raise RuntimeError("Handoff could not be created")
    if not created:
        # Already with support (concurrent request) - behave like a paused-AI turn.
        return customer_message(handoff, sid, conversation_id, user_msg, customer_id)

    logger.info(f"Handoff queued: id={handoff['id']} reason={reason} sid={sid} customer_id={customer_id}")

    chat_repository.save_conversation_message(
        sid, conversation_id, "user", user_msg, domain=DOMAIN, customer_id=customer_id, handoff_id=handoff["id"],
    )
    repository.touch_customer_message(handoff["id"])

    state = _request_assignment(handoff)

    parts = []
    preface = detector.strip_support_contact(ai_answer or "", reason)
    if preface:
        parts.append(preface)
    parts.append(TRANSFER_TEXT)
    if state["status"] == "waiting" and state.get("queue_position"):
        parts.append(QUEUE_TEXT.format(position=state["queue_position"]))
    elif state["status"] == "waiting":
        parts.append(CONNECTING_TEXT)
    answer = "\n\n".join(parts)

    chat_repository.save_conversation_message(
        sid, conversation_id, "assistant", answer, domain=DOMAIN, customer_id=customer_id, handoff_id=handoff["id"],
    )

    return _response(answer, sid, conversation_id, handoff["id"], state)


def customer_message(handoff: dict, sid: str, conversation_id: str, user_msg: str, customer_id: int | None) -> dict:
    """The AI stays silent while a human owns the conversation."""
    message_id = chat_repository.save_conversation_message(
        sid, conversation_id, "user", user_msg, domain=DOMAIN, customer_id=customer_id, handoff_id=handoff["id"],
    )
    repository.touch_customer_message(handoff["id"])
    if handoff.get("agent_id"):
        # Tell the assigned agent's screen right away (Reverb). Off the
        # request thread: the customer's reply must not wait on it.
        threading.Thread(
            target=realtime.publish,
            args=(
                [realtime.agent_channel(handoff["agent_id"])],
                {"handoff_id": handoff["id"], "type": "message", "status": handoff.get("status")},
            ),
            daemon=True,
        ).start()
    response = _response("", sid, conversation_id, handoff["id"], _state_of(handoff))
    # The widget turns the customer's bubble from "sending" into "sent" (✓).
    response["message_id"] = message_id
    return response


def updates(sid: str, customer_id: int | None, handoff_id: int | None, after_id: int) -> dict:
    """Widget polling. Returns the active (or the asked-for, just closed)
    handoff with agent/system messages newer than after_id."""
    handoff = None
    if handoff_id:
        candidate = repository.get_by_id(handoff_id)
        if candidate and _owned_by(candidate, sid, customer_id):
            handoff = candidate
    if not handoff:
        handoff = repository.get_active(sid, customer_id)
    if not handoff:
        return {"active": False, "handoff": None, "messages": []}

    state = _state_of(handoff)
    return {
        "active": state["status"] in ("waiting", "open"),
        "handoff": {
            "id": handoff["id"],
            **state,
            "poll_ms": POLL_INTERVAL_MS,
            # Newest message id the agent has on screen: customer bubbles up to it show "Seen".
            "agent_last_read_id": int(handoff.get("agent_last_read_id") or 0),
        },
        "messages": repository.support_messages_after(handoff["id"], max(0, int(after_id or 0))),
        "realtime": realtime.client_config(),
    }


def mark_read(handoff_id: int, sid: str, customer_id: int | None, last_id: int) -> bool | None:
    """The customer has the agent's messages on screen up to last_id. Moves
    customer_last_read_id forward and tells the agent's screen ("Seen").
    Returns None when the handoff is not this customer's, else whether the
    pointer moved."""
    handoff = repository.get_by_id(handoff_id)
    if not handoff or not _owned_by(handoff, sid, customer_id):
        return None
    if handoff.get("status") != "open":
        return False
    moved = repository.mark_customer_read(handoff_id, last_id)
    if moved and handoff.get("agent_id"):
        threading.Thread(
            target=realtime.publish,
            args=(
                [realtime.agent_channel(handoff["agent_id"])],
                {"handoff_id": handoff_id, "type": "read", "status": handoff.get("status")},
            ),
            daemon=True,
        ).start()
    return moved


def can_access(handoff_id: int, sid: str, customer_id: int | None) -> bool:
    """Whether this customer/session may follow the handoff (realtime auth)."""
    handoff = repository.get_by_id(handoff_id)
    return bool(handoff) and _owned_by(handoff, sid, customer_id)


def _owned_by(handoff: dict, sid: str, customer_id: int | None) -> bool:
    if customer_id and handoff.get("customer_id") == customer_id:
        return True
    return handoff.get("session_id") == sid


def _state_of(handoff: dict) -> dict:
    status = handoff.get("status")
    return {
        "status": status,
        "queue_position": repository.queue_position(handoff) if status == "waiting" else None,
        "agent_name": repository.agent_name(handoff.get("agent_id")) if status == "open" else None,
    }


def _response(answer: str, sid: str, conversation_id: str, handoff_id: int, state: dict) -> dict:
    return {
        "answer": answer,
        "sid": sid,
        "conversation_id": conversation_id,
        "products": [],
        "cart_action": None,
        "mode": "human",
        "handoff": {"id": handoff_id, **state, "poll_ms": POLL_INTERVAL_MS},
        # Socket settings for instant agent replies (None = poll only).
        "realtime": realtime.client_config(),
    }


def _request_assignment(handoff: dict) -> dict:
    """Asks naheedopsv2 to assign an available agent now. If the call fails
    the handoff simply waits for naheedopsv2's every-minute assignment run."""
    if OPS_BASE_URL and NIABOT_HANDOFF_SECRET:
        path = f"/niabot/handoffs/{handoff['id']}/assign"
        timestamp = str(int(time.time()))
        signature = hmac.new(
            NIABOT_HANDOFF_SECRET.encode(), f"{timestamp}.{path}".encode(), hashlib.sha256
        ).hexdigest()
        try:
            res = requests.post(
                OPS_BASE_URL + path,
                headers={
                    "X-NiaBot-Timestamp": timestamp,
                    "X-NiaBot-Signature": signature,
                    "Accept": "application/json",
                },
                timeout=OPS_REQUEST_TIMEOUT,
            )
            if res.ok:
                data = res.json()
                return {
                    "status": data.get("status") or "waiting",
                    "queue_position": data.get("queue_position"),
                    "agent_name": data.get("agent_name"),
                }
            logger.warning(f"naheedopsv2 assign call failed: HTTP {res.status_code} (handoff={handoff['id']})")
        except Exception as e:
            logger.warning(f"naheedopsv2 assign call failed: {e} (handoff={handoff['id']})")
    else:
        logger.warning("OPS_BASE_URL / NIABOT_HANDOFF_SECRET not set - handoff waits for the ops scheduler")

    fresh = repository.get_by_id(handoff["id"]) or handoff
    return _state_of(fresh)
