"""app.py - npk_combine_chatbot entrypoint.

Single Flask app serving one unified chat UI backed by two domains:
  - shopping   (domains/shopping)   - Typesense product search
  - operations (domains/operations) - order tracking, complaints, refunds,
                                       modify/cancel order, etc. (ported from
                                       npk_operation_chatbot's core/flows/services)

Every chat message comes in through ONE endpoint (/api/chat); router/domain_router.py
decides per-message which domain handles it, so the two bots behave as a
single conversation rather than separate tools bolted together.
"""

import uuid

from flask import Flask, jsonify, render_template, request

import config.base as base_config
from domains.shopping import repository as shopping_repository
from domains.shopping.routes import shopping_bp
from domains.operations.routes import operations_bp
from router import domain_router

app = Flask(__name__)
app.secret_key = base_config.FLASK_SECRET_KEY

app.register_blueprint(shopping_bp)
app.register_blueprint(operations_bp)

# Open the DB connection pool at startup - creating it takes ~15s (every
# connection is opened up front), which would otherwise stall the first chat.
from database.connection import get_connection_pool
get_connection_pool()


@app.route("/")
def index():
    # sid/conversation_id are minted and persisted client-side (localStorage)
    # and sent with every /api/chat call — see templates/index.html.
    return render_template("index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    user_msg = (data.get("message") or "").strip()
    sid = data.get("sid") or str(uuid.uuid4())
    client_conv_id = (data.get("conversation_id") or "").strip() or None

    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    # Fallback id for cart/session + history bookkeeping when neither the
    # client nor the domain that ends up handling this message produces a
    # real conversation id (e.g. the very first message of a session is an
    # operations query like "track order 123456").
    fallback_conv_id = client_conv_id or str(uuid.uuid4())
    shopping_repository.ensure_cart_session(sid, fallback_conv_id)

    result = domain_router.route_message(sid, user_msg, client_conv_id)
    conversation_id = result.get("conversation_id") or fallback_conv_id

    shopping_repository.save_conversation_message(sid, conversation_id, "user", user_msg)
    shopping_repository.save_conversation_message(
        sid, conversation_id, "assistant", result["answer"],
        products=result.get("products"), cart_action=result.get("cart_action"),
    )

    return jsonify({
        "answer":          result["answer"],
        "sid":             sid,
        "conversation_id": conversation_id,
        "products":        result.get("products") or [],
        "cart_action":     result.get("cart_action"),
    })


if __name__ == "__main__":
    app.run(debug=base_config.FLASK_DEBUG, port=base_config.FLASK_PORT)
