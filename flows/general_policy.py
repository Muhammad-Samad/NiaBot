from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState


class GeneralPolicyFlow(BaseFlow):
    """Policy / FAQ questions are answered by the policy domain
    (domains/policy, ChromaDB RAG), not by operations. The router sends most
    of them there directly; this flow only catches the ones that still reach
    operations and get classified `general_policy`, and hands them over via
    the `policy_rag` tool request (executed in ConversationManager, which
    knows the session id the policy domain needs)."""

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        user_msg = ""
        if state.conversation_history:
            user_msg = state.conversation_history[-1].get("content", "")

        return FlowResponse(
            status="completed",
            response="",
            tool_request="policy_rag",
            tool_args={"question": user_msg},
        )
