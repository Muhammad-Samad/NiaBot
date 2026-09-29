"""routes.py - Operations domain HTTP endpoints.

Chat itself goes through the unified /api/chat endpoint in app.py (see
router/domain_router.py). The only operations-specific route is the image
upload used by the complaint flow.
"""

import os
import uuid

from flask import Blueprint, jsonify, request

operations_bp = Blueprint("operations", __name__, url_prefix="/api")

# UPLOAD_DIR = os.path.join("static", "uploads")
UPLOAD_DIR = os.path.join("uploads", "tickets", "attachments")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}


@operations_bp.route("/upload", methods=["POST"])
def upload_file():
    """Image upload for complaint attachments. The frontend uploads the file
    first, then sends the returned URL back as a chat message
    ("[Image Uploaded: <url>]") — core/conversation_manager.py special-cases
    that exact prefix when enforcing the message word limit."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Unsupported file type"}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    unique_filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, f"ticketattachment_{unique_filename}")
    file.save(file_path)

    # return jsonify({"url": f"/static/uploads/{unique_filename}"})
    return jsonify({"url": f"/uploads/tickets/attachments/ticketattachment_{unique_filename}"})
