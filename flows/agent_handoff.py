from typing import Dict, Any
from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from services.audit_service import AuditService
from core.audit_events import AuditEvent, AuditCategory, AuditOutcome

class AgentHandoffFlow(BaseFlow):
    def __init__(self):
        super().__init__()
        self.audit_service = AuditService()

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        self.audit_service.log_event(
            event_type=AuditEvent.HUMAN_HANDOFF,
            category=AuditCategory.LIFECYCLE,
            outcome=AuditOutcome.SUCCESS,
            actor="user",
            metadata={"flow_name": "agent_handoff"}
        )
        # Clear the state so the user is not trapped
        state.current_flow = None
        state.current_stage = None
        state.waiting_for_order_id = False
        
        return FlowResponse(
            status="completed",
            response="For further assistance, please contact our Customer Support team at (021) 111-624-333.",
            tool_request="agent_handoff"
        )

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        # Agent Handoff is a terminal flow. It does not continue.
        return False
