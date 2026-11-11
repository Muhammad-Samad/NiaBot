"""routes.py - Shopping domain HTTP endpoints: chat history restore.

Chat itself is NOT a route here — it's reached through the unified /api/chat
endpoint in app.py, which asks router/domain_router.py to decide whether a
message belongs to the shopping or operations domain and calls
domains.shopping.service.handle_message() accordingly.

NiaBot has no cart or order endpoints of its own: a product card's "+ Add"
goes straight into the host website's (Magento) cart via the embed widget
(static/niabot-widget.js), and checkout happens on that website.
"""

import uuid

from flask import Blueprint, jsonify, request

from domains.shopping import repository
from services import memory_service

shopping_bp = Blueprint("shopping", __name__, url_prefix="/api")


@shopping_bp.route("/history", methods=["GET"])
def get_history():
    """Full server-side chat restore, keyed by client-supplied sid — covers
    both shopping and operations turns, since both are persisted through the
    same repository.save_conversation_message().

    For a verified logged-in customer (?ct=), the chat follows the customer
    across devices/browsers: their most recent session is returned instead
    of the browser's own sid (unless they just pressed "New Chat"), and a
    sid owned by a DIFFERENT customer (shared PC) is never shown - a fresh
    sid is handed out instead. The client adopts the returned "sid"."""
    sid = (request.args.get("sid") or "").strip()
    if not sid:
        return jsonify({"error": "Missing sid"}), 400

    # Token only, no device fallback: only a signed customer id may switch sessions.
    key = memory_service.resolve_customer_key(None, request.args.get("ct"))
    if key and key.startswith("cust:"):
        owner = repository.session_owner(sid)
        foreign = bool(owner and owner.startswith("cust:") and owner != key)
        fresh_chat = request.args.get("new_chat") == "1" and not foreign
        if not fresh_chat:
            latest = repository.latest_session_for(key)
            if latest:
                sid = latest
            elif foreign:
                sid = str(uuid.uuid4())

    conversation_id, messages = repository.get_history(sid)
    return jsonify({
        "sid":             sid,
        "conversation_id": conversation_id,
        "messages":        messages,
    })
