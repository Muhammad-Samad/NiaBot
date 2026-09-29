from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from config.base import BOT_NAME

class GoodbyeFlow(BaseFlow):
    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        farewell_msg = (
            f"Thank you for chatting with {BOT_NAME} at Naheed.\n\n"
            "We're always here whenever you need us.\n\n"
            "Stay connected with us:\n\n"
            "Facebook:\n"
            "https://www.facebook.com/naheedpk/\n\n"
            "Instagram:\n"
            "https://www.instagram.com/naheedpkonline/\n\n"
            "Allah Hafiz and have a wonderful day!"
        )
        return FlowResponse(
            status="completed",
            response=farewell_msg,
            updated_state={"current_flow": None},
            end_conversation=True
        )
