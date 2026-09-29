import time
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

class ConversationState(BaseModel):
    current_flow: Optional[str] = None
    current_stage: Optional[str] = None
    waiting_for_order_id: bool = False
    waiting_for_complaint: bool = False
    waiting_for_confirmation: bool = False
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)
    entities: Dict[str, Any] = Field(default_factory=dict)
    last_assistant_message: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)
    # AI-judged priority ("high"/"low") and mood ("happy"/"sad") from the customer's
    # most recent message. Refreshed on every turn by the IntentParser/Router.
    priority: str = "low"
    mood: str = "happy"
    
    # Meta counters for escalation
    consecutive_unknown_count: int = 0
    consecutive_failure_count: int = 0
    
    # Verification state
    customer_verified: bool = False
    verification_attempts: int = 0
    handoff_pending: bool = False
    pending_confirmation: Optional[str] = None

    # Numbered-menu state (in-memory only). "main" while the main menu is
    # awaiting a numbered reply, "policy" while the Policy/FAQ sub-menu is.
    # None once the customer has picked an option or typed free text.
    awaiting_menu: Optional[str] = None

class StateManager:
    def __init__(self, timeout_seconds: int = 300):
        # Maps session_id (e.g. user_id) to ConversationState
        self.sessions: Dict[str, ConversationState] = {}
        self.timeout_seconds = timeout_seconds
        
    def get_state(self, session_id: str = "default") -> ConversationState:
        """Retrieves active state or initializes a new one. Handles context expiration."""
        state = self.sessions.get(session_id)
        if not state:
            state = ConversationState()
            self.sessions[session_id] = state
            return state
            
        # Check timeout (Context Expiration)
        if time.time() - state.timestamp > self.timeout_seconds:
            self.clear_state(session_id)
            return self.sessions[session_id]
            
        # Refresh timestamp on access
        state.timestamp = time.time()
        return state

    def update_state(self, session_id: str, updates: Dict[str, Any]):
        state = self.get_state(session_id)
        for k, v in updates.items():
            if hasattr(state, k):
                setattr(state, k, v)
        state.timestamp = time.time()

    def add_message(self, session_id: str, role: str, content: str):
        """Adds a message to the conversation history."""
        state = self.get_state(session_id)
        state.conversation_history.append({"role": role, "content": content})
        if role == "assistant":
            state.last_assistant_message = content
        state.timestamp = time.time()

    def update_entities(self, session_id: str, new_entities: Dict[str, Any]):
        """Merges new extracted entities into the persistent session."""
        state = self.get_state(session_id)
        # Only update non-null entities
        for k, v in new_entities.items():
            if v is not None:
                # Protect order_id from being overwritten by phone numbers
                if k == "order_id":
                    # If currently in a verification stage, do not overwrite order_id
                    if state.current_stage in ["waiting_for_phone", "ask_verification"]:
                        continue
                    # Also ignore if value looks like a Pakistani phone number (10-12 digits starting with 03 or 92)
                    v_str = str(v).strip()
                    existing_order = state.entities.get("order_id")
                    if existing_order and len(v_str) in [10, 11, 12] and (v_str.startswith("03") or v_str.startswith("92") or v_str.startswith("+92")):
                        continue
                state.entities[k] = v
        state.timestamp = time.time()
        
    def check_cancellation(self, message: str) -> bool:
        """Checks if the user wants to cancel the current workflow."""
        import re
        msg_lower = message.lower().strip()
        
        # Ignore if context is about cancelling an order (business request) rather than chatbot flow
        if re.search(r'\bcancel\s+(?:my\s+|the\s+|this\s+|whole\s+|our\s+)*order\b', msg_lower):
            return False
        if re.search(r'\border\s+(?:cancellation|cancel)\b', msg_lower):
            return False
            
        # Ignore if both "order" and "cancel" appear anywhere in the sentence (Roman Urdu/multilingual fallback)
        if "order" in msg_lower and "cancel" in msg_lower:
            return False
            
        # Check for multi-word phrases first
        for phrase in ["start over", "never mind", "forget it"]:
            if phrase in msg_lower:
                if re.search(r'\b' + re.escape(phrase) + r'\b', msg_lower):
                    return True
                    
        # Check for single-word exact matches
        words = re.findall(r'\b\w+\b', msg_lower)
        for word in words:
            if word in {"cancel", "nevermind", "reset", "abort"}:
                return True
                
        return False

    def clear_state(self, session_id: str = "default"):
        """Resets the conversation to a clean slate."""
        if session_id in self.sessions:
            self.sessions[session_id] = ConversationState()
