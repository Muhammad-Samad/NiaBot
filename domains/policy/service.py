"""service.py - Policy domain message handling.

Answers policy / FAQ / store-information questions with the General_Policy
RAG pipeline (domains/policy/rag/: ChromaDB retrieval + gpt-4o-mini, see
config/policy.py). One PolicyChatbot instance is shared for the app's
lifetime; its per-session follow-up memory is keyed by the same client `sid`
the other domains use.

Reached from three places:
  - router/domain_router.py, for free-text policy questions (including ones
    asked in the middle of an operations flow) and Policy & FAQ menu picks;
  - router/domain_router.py again, for messages the shopping domain judged
    off-topic;
  - core/conversation_manager.py, when the operations intent parser still
    classifies a message as `general_policy` (its `policy_rag` tool).

Persistence: like an operations turn, every policy turn is written to
chatbot_messages through ConversationService (with the RAG sources in
`metadata`), on top of the conversation_messages row app.py writes for every
turn. ask() leaves persistence to the caller, which is how the operations
path saves the turn itself.
"""

import threading

from config.policy import POLICY_MODEL, settings
from services.conversation_service import ConversationService
from utils.logger import get_logger

logger = get_logger(__name__)

POLICY_INTENT = "policy_query"
POLICY_FLOW = "policy"

_ERROR_REPLY = (
    "Sorry, I'm having trouble answering that right now. "
    f"Please try again shortly or contact Naheed support at {settings.support_phone}."
)

_bot = None
_bot_lock = threading.Lock()
_conversation_service = ConversationService()


def _get_bot():
    global _bot
    if _bot is None:
        with _bot_lock:
            if _bot is None:
                # Imported here so chromadb only loads when the policy domain
                # is first used (or warmed up at startup).
                from domains.policy.rag import PolicyChatbot
                _bot = PolicyChatbot()
    return _bot


def warm_up() -> None:
    """Opens the ChromaDB index at startup and logs its size, so a missing
    index or API key shows up in the startup log rather than on the first
    customer question. Never raises - the rest of the app must still start."""
    try:
        chunks = _get_bot().retriever.store.count()
        if chunks:
            logger.info(f"Policy domain ready: {chunks} chunks in '{settings.collection_name}', model={POLICY_MODEL}")
        else:
            logger.warning("Policy index is empty - run: python -m scripts.ingest_policy")
    except Exception as e:
        logger.error(f"Policy domain failed to initialise: {e}")


def health() -> dict:
    chunks = _get_bot().retriever.store.count()
    return {
        "status": "ok" if chunks else "empty_index",
        "chunks": chunks,
        "collection": settings.collection_name,
        "model": POLICY_MODEL,
    }


def ask(sid: str, message: str, entry: str = "free_text") -> dict:
    """Runs the RAG pipeline. Returns {"answer", "metadata"} where metadata
    holds what's worth persisting about the answer (sources, grounding).
    `entry` records how the message reached the policy domain."""
    try:
        reply = _get_bot().ask(message, session_id=sid)
    except Exception as e:
        logger.exception(f"Policy RAG failed: {e}")
        return {
            "answer": _ERROR_REPLY,
            "metadata": {"domain": "policy", "entry": entry, "model": POLICY_MODEL, "error": str(e)},
        }

    if not reply.grounded:
        logger.info(f"Policy answer not grounded in any retrieved chunk (entry={entry})")

    return {
        "answer": reply.answer,
        "metadata": {
            "domain": "policy",
            "entry": entry,
            "model": POLICY_MODEL,
            "grounded": reply.grounded,
            "standalone_question": reply.standalone_question,
            "sources": [
                {"section": s.section, "subsection": s.subsection, "question": s.question, "score": s.score}
                for s in reply.sources
            ],
        },
    }


def handle_message(sid: str, message: str, conversation_id: str | None = None,
                   entry: str = "free_text", question: str | None = None,
                   suffix: str | None = None) -> dict:
    """`question` is what's actually asked of the RAG when it differs from
    what the customer typed (a Policy & FAQ menu pick arrives as just "3").
    `suffix` is appended to the answer - the "continue where we left off"
    note when a policy question interrupts an operations flow."""
    result = ask(sid, question or message, entry=entry)
    answer = result["answer"]
    if suffix:
        answer = f"{answer}\n\n{suffix}"

    _conversation_service.save_user_message(
        session_id=sid, message=message, intent=POLICY_INTENT, flow_name=POLICY_FLOW,
        metadata={"domain": "policy", "entry": entry, **({"question": question} if question else {})},
    )
    _conversation_service.save_bot_message(session_id=sid, message=answer, metadata=result["metadata"])

    return {
        "answer": answer,
        "products": [],
        "cart_action": None,
        # The Typesense conversation thread is untouched by a policy turn -
        # pass it straight through so shopping follow-ups keep working.
        "conversation_id": conversation_id,
        "domain": "policy",
    }
