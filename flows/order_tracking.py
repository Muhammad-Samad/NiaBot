from typing import Optional
from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from services.order_service import OrderService
from utils.logger import get_logger

logger = get_logger(__name__)

class OrderTrackingFlow(BaseFlow):
    """
    Handles the order tracking workflow with phone number verification.
    """
    def __init__(self, order_service: Optional[OrderService] = None):
        self.order_service = order_service or OrderService()

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        if state.current_flow == "order_tracking":
            if state.current_stage == "ask_verification":
                return intent_result.intent in ("order_tracking", "unknown", "general_query", "agent_handoff")
            else:
                if intent_result.intent == "order_tracking":
                    return True
                entities = intent_result.entities if isinstance(intent_result.entities, dict) else intent_result.entities.model_dump()
                if entities.get("order_id"):
                    return True
        return False

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        # Initialize flow if needed
        if state.current_flow != "order_tracking":
            state.current_flow = "order_tracking"
            state.current_stage = "ask_order_id"
            state.waiting_for_order_id = True

        entities = intent_result.entities if isinstance(intent_result.entities, dict) else intent_result.entities.model_dump()
        order_id = entities.get("order_id") or state.entities.get("order_id")

        if state.current_stage == "ask_order_id":
            if not order_id:
                state.waiting_for_order_id = True
                return FlowResponse(
                    status="waiting_for_input",
                    response="I can help you track your order. Please provide your Order ID."
                )
            else:
                state.entities["order_id"] = order_id
                state.current_stage = "check_eligibility"
                state.waiting_for_order_id = False

        if state.current_stage == "check_eligibility":
            # Cheap existence check only - the full order is fetched once by
            # track_order after verification.
            try:
                exists = self.order_service.repository.order_exists(order_id)
            except Exception:
                exists = False
            if not exists:
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response="We couldn't find an order with that ID."
                )
            
            if getattr(state, "customer_verified", False):
                state.current_flow = None
                state.current_stage = None
                return FlowResponse(
                    status="completed",
                    response="Looking up your order status...",
                    updated_state={"current_flow": None, "waiting_for_order_id": False},
                    tool_request="track_order",
                    tool_args={"order_id": order_id}
                )
            else:
                state.current_stage = "ask_verification"
                return FlowResponse(
                    status="waiting_for_input",
                    response="For security purposes, please provide the phone number associated with this order."
                )

        if state.current_stage == "ask_verification":
            user_message = state.conversation_history[-1]["content"] if state.conversation_history else ""
            if self.order_service.verify_customer(order_id, user_message):
                state.customer_verified = True
                state.verification_attempts = 0
                state.current_flow = None
                state.current_stage = None
                return FlowResponse(
                    status="completed",
                    response="Looking up your order status...",
                    updated_state={"current_flow": None, "waiting_for_order_id": False},
                    tool_request="track_order",
                    tool_args={"order_id": order_id}
                )
            else:
                state.verification_attempts += 1
                if state.verification_attempts >= 3:
                    state.current_flow = None
                    state.current_stage = None
                    state.verification_attempts = 0
                    return FlowResponse(
                        status="completed",
                        response="We were unable to verify the provided phone number. I'm connecting you with a customer support representative for further assistance.",
                        tool_request="agent_handoff"
                    )
                else:
                    return FlowResponse(
                        status="waiting_for_input",
                        response="The phone number provided does not match our records. Please try again."
                    )
