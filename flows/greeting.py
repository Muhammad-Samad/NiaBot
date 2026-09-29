from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from config.base import BOT_NAME, BOT_FULL_NAME

class GreetingFlow(BaseFlow):
    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        return FlowResponse(
            status="completed",
            response=(
                f"Hello! I'm {BOT_NAME}, your {BOT_FULL_NAME}. "
                "I can help you find products, track your orders, and register or follow up on complaints. "
                "How can I help you today?"
            ),
            updated_state={"current_flow": None}
        )
