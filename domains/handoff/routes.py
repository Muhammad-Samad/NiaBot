"""routes.py - Handoff HTTP endpoint for the chat widget.

GET /api/handoff/updates?sid=&ct=&device_id=&handoff_id=&after_id=
    Polled by templates/index.html while a conversation is with a human agent
    (and once on page load). Returns the handoff status (queue position /
    agent name) and agent/system messages newer than after_id.

POST /api/handoff/read      (json: sid, ct, device_id, handoff_id, last_id)
    The customer has the agent's messages on screen up to last_id: the
    agent sees "Seen" on their replies (sent only while the widget is visible).

POST /api/handoff/ws-auth   (form: socket_id, channel_name, sid, ct, device_id)
    Reverb private-channel authorization for the widget's socket: the
    customer may only listen on private-handoff.{id} of their own handoff.

Same identity rules as /api/chat and /api/history: the signed customer
token (ct) identifies a logged-in customer, otherwise the sid itself.
"""

from flask import Blueprint, jsonify, request

from domains.handoff import realtime, service
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


@handoff_bp.route("/read", methods=["POST"])
def read():
    data = request.get_json(silent=True) or {}
    sid = (data.get("sid") or "").strip()
    try:
        handoff_id = int(data.get("handoff_id") or 0)
        last_id = int(data.get("last_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400
    if not sid or handoff_id <= 0 or last_id <= 0:
        return jsonify({"error": "Invalid parameters"}), 400
    if not service.enabled():
        return jsonify({"updated": False})

    customer_key = memory_service.resolve_customer_key(data.get("device_id"), data.get("ct"))
    customer_id = memory_service.customer_id_from_key(customer_key)
    try:
        updated = service.mark_read(handoff_id, sid, customer_id, last_id)
    except Exception as e:
        logger.error(f"Handoff read failed (sid={sid}): {e}")
        return jsonify({"error": "unavailable"}), 503
    if updated is None:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({"updated": updated})


@handoff_bp.route("/ws-auth", methods=["POST"])
def ws_auth():
    """Signs the widget's subscription to private-handoff.{id} (pusher-js
    channelAuthorization) after checking the customer owns that handoff."""
    form = request.form
    sid = (form.get("sid") or "").strip()
    channel_name = form.get("channel_name") or ""
    handoff_id = realtime.handoff_id_from_channel(channel_name)
    if not sid or not handoff_id or not realtime.enabled():
        return jsonify({"error": "Forbidden"}), 403

    customer_key = memory_service.resolve_customer_key(form.get("device_id"), form.get("ct"))
    customer_id = memory_service.customer_id_from_key(customer_key)
    try:
        allowed = service.can_access(handoff_id, sid, customer_id)
    except Exception as e:
        logger.error(f"Realtime auth lookup failed (sid={sid}): {e}")
        return jsonify({"error": "unavailable"}), 503
    if not allowed:
        return jsonify({"error": "Forbidden"}), 403

    auth = realtime.authorize(form.get("socket_id") or "", channel_name)
    if not auth:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(auth)
