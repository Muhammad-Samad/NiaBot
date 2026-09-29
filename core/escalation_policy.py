from typing import Dict, Any
from ai.schemas import IntentResult
from core.state_manager import ConversationState

class EscalationDecision:
    def __init__(self, action: str, reason: str = "none"):
        # action can be: "continue", "offer_escalation", "immediate_handoff"
        self.action = action
        self.reason = reason

class EscalationPolicy:
    """
    Evaluates the current state of the conversation and the LLM's recommendation
    to determine if the user should be connected to a human agent.
    """
    def __init__(self, max_failed_attempts=3, max_tool_failures=2):
        self.max_failed_attempts = max_failed_attempts
        self.max_tool_failures = max_tool_failures

    def evaluate(self, intent_result: IntentResult, state: ConversationState) -> EscalationDecision:
        # 1. Explicit request for human agent
        if getattr(intent_result, "intent", None) == "agent_handoff":
            return EscalationDecision("immediate_handoff", "explicit_request")
            
        # 2. LLM specifically recommended an escalation
        if getattr(intent_result, "escalation_recommended", False):
            reason = getattr(intent_result, "escalation_reason", "customer_frustration")
            return EscalationDecision("offer_escalation", reason)

        # 3. Check for multiple unknown/failed attempts to understand the user
        if state.failed_attempts >= self.max_failed_attempts:
            return EscalationDecision("offer_escalation", "multiple_failed_attempts")

        # 4. Check for repeated backend service/tool failures
        if state.tool_failures >= self.max_tool_failures:
            return EscalationDecision("offer_escalation", "backend_failure")

        return EscalationDecision("continue", "none")
