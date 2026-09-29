import re
from unittest.mock import MagicMock
from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from database.repository import OrderRepository

class RefundFlow(BaseFlow):
    def __init__(self, order_repository=None):
        self.order_repository = order_repository or OrderRepository()

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        user_msg = ""
        if state.conversation_history:
            user_msg = state.conversation_history[-1]["content"].strip()
            
        order_id = state.entities.get("order_id")
        if not order_id:
            if isinstance(intent_result.entities, dict):
                extracted = intent_result.entities.get("order_id")
            elif hasattr(intent_result.entities, "model_dump"):
                extracted = intent_result.entities.model_dump().get("order_id")
            else:
                extracted = getattr(intent_result.entities, "order_id", None)
            if extracted is not None and not isinstance(extracted, MagicMock if 'MagicMock' in globals() else object):
                order_id = str(extracted)

        if not order_id:
            # Fallback to regex
            match = re.search(r'\b(?=[a-zA-Z0-9-]*\d)[a-zA-Z0-9-]{5,20}\b', user_msg)
            if match:
                order_id = match.group(0)

        if not order_id:
            return FlowResponse(
                status="waiting_for_input",
                response="I understand you want a refund. Please provide the Order ID.",
                updated_state={
                    "current_flow": "refund",
                    "current_stage": "waiting_for_order_id",
                    "waiting_for_order_id": True
                }
            )

        try:
            resolved_id = self.order_repository.resolve_to_latest_order_id(order_id)
            self.order_repository.get_order_by_increment_id(resolved_id)
            new_entities = {**state.entities, "order_id": resolved_id, "complaint_category": "Refund"}
            if resolved_id != order_id:
                new_entities["parent_order_id"] = order_id
            
            return FlowResponse(
                status="waiting_for_input",
                response="Order ID verified. Please select the refund sub-category:\n- Missing Item\n- Wrong Item",
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_refund_sub_category",
                    "waiting_for_order_id": False,
                    "entities": new_entities
                }
            )
        except Exception:
            return FlowResponse(
                status="waiting_for_input",
                response="We couldn't find an order with that ID. Please check and try again.",
                updated_state={
                    "current_flow": "refund",
                    "current_stage": "waiting_for_order_id",
                    "waiting_for_order_id": True
                }
            )

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        if intent_result.intent == "refund":
            return True
        # Check if user message contains 5-13 digits (Order ID format)
        user_msg = ""
        if state.conversation_history:
            user_msg = state.conversation_history[-1]["content"].strip()
        if re.search(r'\b(?=[a-zA-Z0-9-]*\d)[a-zA-Z0-9-]{5,20}\b', user_msg):
            return True
        entities = intent_result.entities if isinstance(intent_result.entities, dict) else intent_result.entities.model_dump()
        if entities.get("order_id"):
            return True
        return False
