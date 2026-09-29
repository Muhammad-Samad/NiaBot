from typing import Dict, Type
from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState

# Import all flows
from flows.order_tracking import OrderTrackingFlow
from flows.complaint import ComplaintFlow
from flows.complaint_tracking import ComplaintTrackingFlow
from flows.refund import RefundFlow
from flows.general_policy import GeneralPolicyFlow
from flows.general_query import GeneralQueryFlow
from flows.greeting import GreetingFlow
from flows.goodbye import GoodbyeFlow
from flows.unknown import UnknownFlow
from flows.modify_order import ModifyOrderFlow
from flows.cancel_order import CancelOrderFlow
from flows.agent_handoff import AgentHandoffFlow
from flows.special_request import SpecialRequestFlow

class FlowManager:
    """
    Acts as the orchestration layer mapping intents to their respective conversational flows.
    """
    def __init__(self, order_service=None, order_repository=None):
        # Register flows
        self._flows: Dict[str, BaseFlow] = {
            "order_tracking": OrderTrackingFlow(order_service=order_service),
            "modify_order": ModifyOrderFlow(),
            "complaint": ComplaintFlow(order_repository=order_repository),
            "complaint_tracking": ComplaintTrackingFlow(order_service=order_service),
            "refund": RefundFlow(),
            "general_policy": GeneralPolicyFlow(),
            "general_query": GeneralQueryFlow(),
            "greeting": GreetingFlow(),
            "goodbye": GoodbyeFlow(),
            "unknown": UnknownFlow(),
            "cancel_order": CancelOrderFlow(),
            "agent_handoff": AgentHandoffFlow(),
            "special_request": SpecialRequestFlow()
        }
        
    def execute_flow(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        if state.current_flow:
            active_flow = self._flows.get(state.current_flow)
            if active_flow and active_flow.is_continuation(intent_result, state):
                flow_name = state.current_flow
            else:
                # Interruption: The message is not a continuation.
                # Clear active tracking state to prevent user from being trapped.
                state.current_flow = None
                state.current_stage = None
                state.waiting_for_order_id = False
                flow_name = intent_result.intent
        else:
            flow_name = intent_result.intent
        
        flow_instance = self._flows.get(flow_name)
        if not flow_instance:
            # Fallback for unregistered intents
            flow_instance = self._flows["unknown"]
            
        return flow_instance.handle(intent_result, state)
