"""routes.py - Shopping domain HTTP endpoints: cart and order placement.

Chat itself is NOT a route here — it's reached through the unified /api/chat
endpoint in app.py, which asks router/domain_router.py to decide whether a
message belongs to the shopping or operations domain and calls
domains.shopping.service.handle_message() accordingly. Only the
shopping-specific cart/order endpoints (which the unified chat UI's cart
sidebar calls directly) live here.
"""

import uuid

from flask import Blueprint, jsonify, request

from domains.shopping import repository
from services import memory_service

shopping_bp = Blueprint("shopping", __name__, url_prefix="/api")


@shopping_bp.route("/cart", methods=["GET"])
def get_cart():
    sid = (request.args.get("sid") or "").strip()
    if not sid:
        return jsonify({"error": "Missing sid"}), 400

    items, total = repository.get_cart_totals(sid)
    return jsonify({"items": items, "total": total})


@shopping_bp.route("/cart/add", methods=["POST"])
def add_to_cart():
    data = request.get_json(force=True)
    sid = (data.get("sid") or "").strip()
    if not sid:
        return jsonify({"error": "Missing sid"}), 400

    product_id = str(data.get("product_id") or "").strip()
    product_name = str(data.get("product_name") or "").strip()
    if not product_id or not product_name:
        return jsonify({"error": "Missing product_id or product_name"}), 400

    price = data.get("price", 0) or 0
    quantity = max(1, int(data.get("quantity", 1) or 1))
    sku = str(data.get("sku") or "")
    image_url = str(data.get("image_url") or "")
    category = str(data.get("category") or "")

    repository.add_to_cart(sid, product_id, product_name, price, quantity, sku, image_url, category)
    return jsonify({"success": True})


@shopping_bp.route("/cart/remove", methods=["POST"])
def remove_from_cart():
    data = request.get_json(force=True)
    sid = (data.get("sid") or "").strip()
    product_id = str(data.get("product_id") or "").strip()
    if not sid or not product_id:
        return jsonify({"error": "Missing sid or product_id"}), 400

    repository.remove_from_cart(sid, product_id)
    return jsonify({"success": True})


@shopping_bp.route("/cart/clear", methods=["POST"])
def clear_cart():
    data = request.get_json(silent=True) or {}
    sid = (data.get("sid") or request.args.get("sid") or "").strip()
    if not sid:
        return jsonify({"error": "Missing sid"}), 400

    repository.clear_cart(sid)
    return jsonify({"success": True})


@shopping_bp.route("/order/place", methods=["POST"])
def place_order():
    data = request.get_json(force=True)
    sid     = (data.get("sid") or "").strip()
    name    = (data.get("name") or "").strip()
    phone   = (data.get("phone") or "").strip()
    address = (data.get("address") or "").strip()

    if not sid:
        return jsonify({"error": "Missing sid"}), 400
    if not name or not phone:
        return jsonify({"success": False, "error": "Name and phone are required"}), 400

    result = repository.place_order(sid, name, phone, address)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


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
