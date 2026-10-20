"""app.py - npk_combine_chatbot entrypoint.

Single Flask app serving one unified chat UI backed by three domains:
  - shopping   (domains/shopping)   - Typesense product search
  - operations (domains/operations) - order tracking, complaints, refunds,
                                       modify/cancel order, etc. (ported from
                                       npk_operation_chatbot's core/flows/services)
  - policy     (domains/policy)     - policy / FAQ / store-information answers
                                       from the General_Policy RAG pipeline
                                       (ChromaDB + gpt-4o-mini)

Every chat message comes in through ONE endpoint (/api/chat); router/domain_router.py
decides per-message which domain handles it, so the bots behave as a
single conversation rather than separate tools bolted together.
"""

import uuid

from flask import Flask, jsonify, render_template, request

import config.base as base_config
from utils.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

from domains.shopping import repository as shopping_repository
from domains.shopping.routes import shopping_bp
from domains.operations.routes import attachments_bp, operations_bp
from domains.policy import service as policy_service
from domains.policy.routes import policy_bp
from router import domain_router
from services import memory_service

app = Flask(__name__)
app.secret_key = base_config.FLASK_SECRET_KEY

app.register_blueprint(shopping_bp)
app.register_blueprint(operations_bp)
app.register_blueprint(attachments_bp)
app.register_blueprint(policy_bp)

# Open the DB connection pool at startup - creating it takes ~15s (every
# connection is opened up front), which would otherwise stall the first chat.
from database.connection import get_connection_pool
get_connection_pool()

# Open the policy domain's ChromaDB index up front too, and log its size.
policy_service.warm_up()
logger.info(f"NiaBot started (port={base_config.FLASK_PORT}, debug={base_config.FLASK_DEBUG})")


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

    """ Fallback id for cart/session + history bookkeeping when neither the
     client nor the domain that ends up handling this message produces a
     real conversation id (e.g. the very first message of a session is an
     operations query like "track order 123456")."""
    fallback_conv_id = client_conv_id or str(uuid.uuid4())
    shopping_repository.ensure_cart_session(sid, fallback_conv_id)

    # Long-term memory identity: a signed logged-in customer id from the host
    # site, else the browser's device id (survives "New Chat").
    customer_key = memory_service.resolve_customer_key(data.get("device_id"), data.get("customer_token"))
    memory_service.link_session(sid, customer_key)

    result = domain_router.route_message(sid, user_msg, client_conv_id, customer_key=customer_key)
    conversation_id = result.get("conversation_id") or fallback_conv_id

    domain = result.get("domain")
    shopping_repository.save_conversation_message(sid, conversation_id, "user", user_msg, domain=domain)
    shopping_repository.save_conversation_message(
        sid, conversation_id, "assistant", result["answer"],
        products=result.get("products"), cart_action=result.get("cart_action"), domain=domain,
    )

    return jsonify({
        "answer":          result["answer"],
        "sid":             sid,
        "conversation_id": conversation_id,
        "products":        result.get("products") or [],
        "cart_action":     result.get("cart_action"),
    })


@app.route("/api/welcome", methods=["POST"])
def welcome():
    """Called by the chat UI when it opens a session with no history: a
    "welcome back" line (and a one-tap prompt to continue) for a returning
    customer, else nothing."""
    data = request.get_json(force=True)
    sid = data.get("sid")
    customer_key = memory_service.resolve_customer_key(data.get("device_id"), data.get("customer_token"))
    if not sid or not customer_key:
        return jsonify({})
    shopping_repository.ensure_cart_session(sid)
    memory_service.link_session(sid, customer_key)
    return jsonify(memory_service.welcome(customer_key) or {})


@app.route("/api/memory/forget", methods=["POST"])
def forget_memory():
    data = request.get_json(force=True)
    customer_key = memory_service.resolve_customer_key(data.get("device_id"), data.get("customer_token"))
    return jsonify({"forgotten": memory_service.forget(customer_key)})


if __name__ == "__main__":
    app.run(debug=base_config.FLASK_DEBUG, port=base_config.FLASK_PORT)
