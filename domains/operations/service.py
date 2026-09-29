"""service.py - Operations domain message handling.

Thin wrapper around core.conversation_manager.ConversationManager — the same
orchestration brain npk_operation_chatbot's Streamlit console (developer_console.py)
and FastAPI wrapper (api.py) both called. One ConversationManager instance is
shared for the app's lifetime, exactly as those did.
"""

from core.conversation_manager import ConversationManager

_conversation_manager = ConversationManager()


def is_mid_flow(session_id: str) -> bool:
    """True when this session is in the middle of a stateful operations flow
    (e.g. order tracking waiting for an order id, a complaint collecting
    details). The domain router uses this to keep routing follow-up turns to
    operations even when they wouldn't, on their own, look operational."""
    state = _conversation_manager.state_manager.get_state(session_id)
    return bool(state.current_flow)


def get_menu_stage(session_id: str):
    """Returns "main"/"policy" while a numbered menu is awaiting a reply for
    this session, else None. See router/menu.py."""
    state = _conversation_manager.state_manager.get_state(session_id)
    return state.awaiting_menu


def set_menu_stage(session_id: str, stage) -> None:
    _conversation_manager.state_manager.update_state(session_id, {"awaiting_menu": stage})


def handle_message(session_id: str, message: str, forced_intent: str = None, forced_entities: dict = None) -> dict:
    """`forced_intent`/`forced_entities` let a numbered-menu selection (see
    router/menu.py) skip LLM/rule-based classification entirely and go
    straight into the matching flow, while still reusing the normal
    escalation/tool-dispatch/persistence pipeline."""
    answer, debug_data = _conversation_manager.process_message_with_debug(
        message, session_id, forced_intent=forced_intent, forced_entities=forced_entities
    )
    return {"answer": answer, "debug": debug_data}
