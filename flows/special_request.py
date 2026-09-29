from typing import Dict, Any
from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from services.audit_service import AuditService
from core.audit_events import AuditEvent, AuditCategory, AuditOutcome
from flows.agent_handoff import AgentHandoffFlow

class SpecialRequestFlow(BaseFlow):
    """
    Handles special operational requests like expediting delivery, prioritizing orders, etc.
    These require manual intervention, so this flow directly hands off to a human agent,
    reusing the exact behavior and response of AgentHandoffFlow.
    """
    def __init__(self):
        super().__init__()
        self.audit_service = AuditService()
        self.handoff_flow = AgentHandoffFlow()

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        original_message = ""
        if state.conversation_history:
            original_message = state.conversation_history[-1].get("content", "")
            
        classification_source = "LLM" if getattr(intent_result, "confidence", 0) > 0 else "Regex"
        
        self.audit_service.log_event(
            event_type=AuditEvent.SPECIAL_REQUEST,
            category=AuditCategory.BUSINESS,
            outcome=AuditOutcome.SUCCESS,
            actor="user",
            metadata={
                "original_message": original_message,
                "classification_source": classification_source,
                "confidence_score": getattr(intent_result, "confidence", 0.0),
                "flow_name": "special_request"
            }
        )
        
        # Reuse the existing handoff logic entirely to ensure single source of truth
        return self.handoff_flow.handle(intent_result, state)

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        return False
