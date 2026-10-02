"""routes.py - Policy domain HTTP endpoints.

Chat itself goes through the unified /api/chat endpoint in app.py (see
router/domain_router.py). The only policy-specific route is a health check
for the ChromaDB index, mirroring General_Policy RAG's /api/health.
"""

from flask import Blueprint, jsonify

from domains.policy import service
from utils.logger import get_logger

logger = get_logger(__name__)

policy_bp = Blueprint("policy", __name__, url_prefix="/api/policy")


@policy_bp.route("/health", methods=["GET"])
def health():
    try:
        return jsonify(service.health())
    except Exception as e:
        logger.exception(f"Policy health check failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500
