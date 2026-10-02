from typing import Optional
import logging
import re
from core.state_manager import ConversationState
from ai.schemas import IntentResult
from flows.base import BaseFlow, FlowResponse
from services.order_service import OrderService

logger = logging.getLogger(__name__)

class ComplaintTrackingFlow(BaseFlow):
    """
    Handles the complaint tracking workflow with phone number verification.
    """
    def __init__(self, order_service: Optional[OrderService] = None):
        super().__init__()
        self.order_service = order_service or OrderService()

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        if state.current_flow != "complaint_tracking":
            return False
        if state.current_stage == "ask_verification":
            return intent_result.intent in ("complaint_tracking", "unknown", "general_query", "agent_handoff")
        return state.waiting_for_order_id

    def _extract_order_id(self, intent_result: IntentResult, state: ConversationState) -> Optional[str]:
        order_id = None

        # Check entities (the intent prompt emits "order_no" for complaint tracking)
        entities = getattr(intent_result, 'entities', None)
        if entities:
            if not isinstance(entities, dict):
                entities = entities.model_dump() if hasattr(entities, 'model_dump') else {}
            order_id = entities.get("order_id") or entities.get("order_no")

        # Check state if not in entities
        if not order_id and state.entities:
            order_id = state.entities.get("order_id")

        # Fallback: regex extraction from last user message if order_id still missing
        if not order_id and state.conversation_history:
            last_message = state.conversation_history[-1].get("content", "")
            match = re.search(r'\b\d{5,15}\b', last_message)
            if match:
                order_id = match.group(0)
                logger.debug(f"ComplaintTrackingFlow: extracted order_id '{order_id}' via regex fallback.")
        return order_id

    def _reset(self, state: ConversationState):
        state.current_flow = None
        state.current_stage = None
        state.waiting_for_order_id = False
        state.verification_attempts = 0

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        logger.debug("ComplaintTrackingFlow.handle() executed")

        # Initialize flow if needed
        if state.current_flow != "complaint_tracking":
            state.current_flow = "complaint_tracking"
            state.current_stage = "ask_order_id"
            state.waiting_for_order_id = True

        if state.current_stage == "ask_order_id":
            order_id = self._extract_order_id(intent_result, state)
            logger.debug(f"ComplaintTrackingFlow: received order_id={order_id}")
            if not order_id:
                logger.debug("ComplaintTrackingFlow: Order Number is missing. Prompting user.")
                state.waiting_for_order_id = True
                return FlowResponse(
                    response="Please provide your Order Number.",
                    status="waiting_for_input"
                )
            state.entities["order_id"] = order_id
            state.current_stage = "check_eligibility"
            state.waiting_for_order_id = False

        # Once the order ID is captured, never re-extract it: the next message
        # is the phone number, which the regex fallback would mistake for one.
        order_id = state.entities.get("order_id")

        if state.current_stage == "check_eligibility":
            try:
                exists = self.order_service.repository.order_exists(order_id)
            except Exception:
                exists = False
            if not exists:
                self._reset(state)
                state.customer_verified = False
                return FlowResponse(
                    status="completed",
                    response="We couldn't find an order with that ID."
                )

            if getattr(state, "customer_verified", False):
                return self._track(order_id, state)

            state.current_stage = "ask_verification"
            return FlowResponse(
                status="waiting_for_input",
                response="For security purposes, please provide the phone number associated with this order."
            )

        if state.current_stage == "ask_verification":
            user_message = state.conversation_history[-1]["content"] if state.conversation_history else ""
            if self.order_service.verify_customer(order_id, user_message):
                state.customer_verified = True
                return self._track(order_id, state)

            state.verification_attempts += 1
            if state.verification_attempts >= 3:
                self._reset(state)
                return FlowResponse(
                    status="completed",
                    response="We were unable to verify the provided phone number. Please contact our Customer Support team at (021) 111-624-333 for further assistance.",
                    tool_request="agent_handoff"
                )
            return FlowResponse(
                status="waiting_for_input",
                response="The phone number provided does not match our records. Please try again."
            )

        # Unknown stage - start over
        state.current_stage = "ask_order_id"
        state.waiting_for_order_id = True
        return FlowResponse(status="waiting_for_input", response="Please provide your Order Number.")

    def _track(self, order_id: str, state: ConversationState) -> FlowResponse:
        logger.debug(f"ComplaintTrackingFlow: continuing with order_id={order_id}")
        self._reset(state)
        return FlowResponse(
            response="",  # ConversationManager will handle appending the service response
            status="completed",
            tool_request="track_complaint",
            tool_args={"order_no": order_id},
            updated_state={
                "current_flow": None,
                "waiting_for_order_id": False
            }
        )
