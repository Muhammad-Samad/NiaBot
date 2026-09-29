from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState

class UnknownFlow(BaseFlow):
    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        return FlowResponse(
            status="completed",
            response="I'm sorry, I didn't quite understand that. Could you please rephrase your request?",
            updated_state={"current_flow": None}
        )
