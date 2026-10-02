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


def get_resume_context(session_id: str):
    """Returns (current_flow, last_assistant_message) so a turn answered by
    another domain mid-flow (a policy question) can remind the customer
    where this flow left off. Doesn't modify the operations state."""
    state = _conversation_manager.state_manager.get_state(session_id)
    return state.current_flow, state.last_assistant_message


# Flow stages that are waiting for an order ID or a phone number. Every flow
# just re-asks when the reply at one of these steps contains no digits.
_IDENTIFIER_STAGES = {"waiting_for_order_id", "waiting_for_phone", "ask_order_id", "ask_verification"}


def is_awaiting_identifier(session_id: str) -> bool:
    """True when the current flow step is asking for an order ID / phone
    number. A reply with no digits at all can't be the answer, so the router
    resets the flow and treats that reply as a brand-new message."""
    state = _conversation_manager.state_manager.get_state(session_id)
    return bool(state.current_flow) and (
        state.waiting_for_order_id or state.current_stage in _IDENTIFIER_STAGES
    )


def reset_flow(session_id: str) -> None:
    """Drops whatever operations flow this session is in, exactly like the
    customer typing "cancel" would (minus the reply)."""
    _conversation_manager.state_manager.clear_state(session_id)


def is_cancellation(message: str) -> bool:
    """True for "cancel" / "reset" / "never mind" etc. - the operations reset
    command, which must keep reaching operations."""
    return _conversation_manager.state_manager.check_cancellation(message)


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
