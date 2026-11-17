"""routes.py - Handoff HTTP endpoint for the chat widget.

GET /api/handoff/updates?sid=&ct=&device_id=&handoff_id=&after_id=
    Polled by templates/index.html while a conversation is with a human agent
    (and once on page load). Returns the handoff status (queue position /
    agent name) and agent/system messages newer than after_id.

Same identity rules as /api/chat and /api/history: the signed customer
token (ct) identifies a logged-in customer, otherwise the sid itself.
"""

from flask import Blueprint, jsonify, request

from domains.handoff import service
from services import memory_service
from utils.logger import get_logger

logger = get_logger(__name__)

handoff_bp = Blueprint("handoff", __name__, url_prefix="/api/handoff")


@handoff_bp.route("/updates", methods=["GET"])
def updates():
    sid = (request.args.get("sid") or "").strip()
    if not sid:
        return jsonify({"error": "Missing sid"}), 400
    if not service.enabled():
        return jsonify({"active": False, "handoff": None, "messages": []})

    customer_key = memory_service.resolve_customer_key(request.args.get("device_id"), request.args.get("ct"))
    customer_id = memory_service.customer_id_from_key(customer_key)

    try:
        handoff_id = int(request.args.get("handoff_id") or 0) or None
        after_id = int(request.args.get("after_id") or 0)
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    try:
        return jsonify(service.updates(sid, customer_id, handoff_id, after_id))
    except Exception as e:
        logger.error(f"Handoff updates failed (sid={sid}): {e}")
        return jsonify({"active": False, "handoff": None, "messages": [], "error": "unavailable"}), 503
