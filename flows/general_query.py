from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from config.base import BOT_NAME, BOT_FULL_NAME

class GeneralQueryFlow(BaseFlow):
    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        return FlowResponse(
            status="completed",
            response=(
                f"I'm {BOT_NAME}, the {BOT_FULL_NAME}. "
                "I can help you find and shop for products, track your orders, and register or track complaints. "
                "What would you like to do?"
            ),
            updated_state={"current_flow": None}
        )
