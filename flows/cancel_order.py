import re
from typing import Optional
from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from services.order_service import OrderService
from database.repository import OrderRepository
from utils.logger import get_logger

logger = get_logger(__name__)

class CancelOrderFlow(BaseFlow):
    """
    Handles the intelligent order cancellation workflow.
    Ensures the order is eligible, asks for the reason, and decides whether
    to auto-cancel or redirect to agent handoff.
    """
    # Ordered list of (code, label) shown to the customer as a numbered menu.
    REASON_OPTIONS = [
        ("ordered_by_mistake", "Ordered by mistake"),
        ("price_negotiation", "Found a better price elsewhere"),
        ("shipping_negotiation", "Shipping charges are too high"),
        ("no_longer_needed", "No longer needed"),
        ("change_of_mind", "Change of mind"),
        ("duplicate_order", "Duplicate order"),
        ("other", "Other"),
    ]

    # Free-text keywords that map onto the same codes, so typed answers still work.
    REASON_KEYWORDS = {
        "ordered_by_mistake": ["mistake", "wrong order", "accidental"],
        "price_negotiation": ["better price", "cheaper", "price"],
        "shipping_negotiation": ["shipping", "delivery charge", "delivery fee"],
        "no_longer_needed": ["no longer need", "don't need", "not needed"],
        "change_of_mind": ["change of mind", "changed my mind"],
        "duplicate_order": ["duplicate", "ordered twice", "double order"],
        "other": ["other"],
    }

    def __init__(self, order_service: Optional[OrderService] = None, order_repository=None):
        self.order_service = order_service or OrderService()
        self.order_repository = order_repository or OrderRepository()

    def _reason_menu_prompt(self, prefix: str) -> str:
        lines = [prefix, "Please choose an option:"]
        for i, (_, label) in enumerate(self.REASON_OPTIONS, start=1):
            lines.append(f"{i}. {label}")
        return "\n".join(lines)

    def _resolve_reason_selection(self, user_message: str, intent_result: IntentResult) -> Optional[str]:
        text = (user_message or "").strip().lower()
        if not text:
            return None

        # Numbered menu selection, e.g. "3", "3.", "option 3".
        match = re.search(r"\d+", text)
        if match:
            idx = int(match.group())
            if 1 <= idx <= len(self.REASON_OPTIONS):
                return self.REASON_OPTIONS[idx - 1][0]

        # Typed keyword match.
        for code, keywords in self.REASON_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return code

        # Fall back to whatever the LLM extracted, if it is one of our known codes.
        llm_reason = getattr(intent_result.entities, "cancel_reason", None)
        valid_codes = {code for code, _ in self.REASON_OPTIONS}
        if llm_reason in valid_codes:
            return llm_reason

        return None

    CSR_OFFER_OPTIONS = (
        "Please choose an option:\n"
        "1. I will contact Customer Support\n"
        "2. I still want to cancel my order"
    )

    def _csr_offer_prompt(self, reason: str) -> str:
        concern = "shipping charges" if reason == "shipping_negotiation" else "the price"
        return (
            f"We're sorry to hear that {concern} didn't meet your expectations. "
            "Before you cancel, please contact our Customer Support team at (021) 111-624-333 - "
            "you may be eligible for a discount in the form of a gift card.\n"
            + self.CSR_OFFER_OPTIONS
        )

    def _resolve_csr_offer_choice(self, user_message: str) -> Optional[str]:
        text = (user_message or "").strip().lower()
        if not text:
            return None

        match = re.search(r"\d+", text)
        if match:
            return {1: "contact_csr", 2: "proceed_cancel"}.get(int(match.group()))

        if any(k in text for k in ["don't cancel", "dont cancel", "do not cancel", "not cancel"]):
            return "contact_csr"
        if any(k in text for k in ["cancel", "proceed", "continue"]):
            return "proceed_cancel"
        if any(k in text for k in ["contact", "support", "call", "csr", "discount", "gift"]):
            return "contact_csr"
        return None

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        if state.current_flow == "cancel_order":
            # If the user explicitly asks to cancel again, or provides a reason, it's a continuation.
            return intent_result.intent in ("cancel_order", "unknown", "general_query", "agent_handoff")
        return False

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        # Initialize state if this is the first turn
        if state.current_flow != "cancel_order":
            state.current_flow = "cancel_order"
            state.current_stage = "ask_order_id"
            state.waiting_for_order_id = True

        order_id = getattr(intent_result.entities, "order_id", None) or state.entities.get("order_id")

        if state.current_stage == "ask_order_id":
            if not order_id:
                state.waiting_for_order_id = True
                return FlowResponse(
                    status="waiting_for_input",
                    response="Could you please provide the Order ID you would like to cancel?"
                )
            else:
                # We have the order ID. Save it to state and transition to checking eligibility.
                state.entities["order_id"] = order_id
                state.current_stage = "check_eligibility"
                state.waiting_for_order_id = False
                # Fall through to process eligibility in the same turn if we just got the ID.
                # However, for simplicity and natural conversation flow, we process it now.

        if state.current_stage == "check_eligibility":
            status_check = self.order_service.get_order_status(order_id)
            if not status_check.get("success"):
                # Order not found or error
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response=status_check.get("message", "We couldn't find an order with that ID.")
                )

            order_status = (status_check.get("status") or "").lower()
            order_state = (status_check.get("state") or "").lower()
            
            if order_state == "canceled" or order_status == "canceled":
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response="This order has already been cancelled. No further action is required."
                )

            if order_status not in ["pending", "approved"]:
                # Cannot cancel
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response="Your order has already entered fulfillment and cannot be cancelled automatically. Please contact to Customer Support team at (021) 111-624-333 for assistance.",
                    tool_request="agent_handoff"
                )
            # dealing prepaid orders 
            payment_method = self.order_repository.get_payment_method(order_id)
            is_prepaid = payment_method.lower() not in ["cashondelivery", "ccondelivery"]
            
            if is_prepaid and order_status == "approved":
                # Cannot cancel
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response="Your order is prepaid, so it cannot be cancelled directly. To process your refund, please contact our Customer Support team at (021) 111-624-333. We’ll be happy to assist you.",
                    tool_request="agent_handoff"
                )
            
            # Eligible. Ask for verification if not already verified.
            if getattr(state, "customer_verified", False):
                state.current_stage = "ask_reason"
                return FlowResponse(
                    status="waiting_for_input",
                    response=self._reason_menu_prompt("Your order is eligible for cancellation. Could you please tell us the reason for cancelling?")
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
                state.current_stage = "ask_reason"
                return FlowResponse(
                    status="waiting_for_input",
                    response=self._reason_menu_prompt("Verification successful. Your order is eligible for cancellation. Could you please tell us the reason for cancelling?")
                )
            else:
                state.verification_attempts += 1
                if state.verification_attempts >= 3:
                    state.current_flow = None
                    state.current_stage = None
                    state.verification_attempts = 0
                    return FlowResponse(
                        status="completed",
                        response="We were unable to verify the provided phone number. Please contact to customer support representative for assistance. ((021) 111-624-333)",
                        tool_request="agent_handoff"
                    )
                else:
                    return FlowResponse(
                        status="waiting_for_input",
                        response="The phone number provided does not match our records. Please try again."
                    )

        if state.current_stage == "ask_reason":
            if intent_result.intent == "agent_handoff" or intent_result.escalation_recommended:
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response="I understand. Please contact to customer support representative who can assist you with this request.",
                    tool_request="agent_handoff"
                )

            user_message = state.conversation_history[-1]["content"] if getattr(state, "conversation_history", None) else ""
            reason = self._resolve_reason_selection(user_message, intent_result)

            if not reason:
                # Selection didn't match a menu option; re-show the menu.
                return FlowResponse(
                    status="waiting_for_input",
                    response=self._reason_menu_prompt("I didn't quite catch that.")
                )

            if reason == "other":
                state.current_stage = "ask_other_reason"
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please tell us the reason for cancelling your order."
                )

            if reason in ["price_negotiation", "shipping_negotiation"]:
                # Offer a CSR contact (possible gift card discount) before cancelling.
                state.entities["pending_cancel_reason"] = reason
                state.current_stage = "offer_csr_discount"
                return FlowResponse(
                    status="waiting_for_input",
                    response=self._csr_offer_prompt(reason)
                )
            else:
                # duplicate_order, ordered_by_mistake, no_longer_needed, change_of_mind: proceed to cancel.
                cancel_result = self.order_service.execute_cancellation(order_id, reason)
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response=cancel_result.get("message", "Your order has been successfully cancelled.")
                )

        if state.current_stage == "offer_csr_discount":
            user_message = state.conversation_history[-1]["content"] if getattr(state, "conversation_history", None) else ""
            choice = self._resolve_csr_offer_choice(user_message)
            reason = state.entities.get("pending_cancel_reason") or "price_negotiation"

            if choice == "contact_csr":
                state.entities.pop("pending_cancel_reason", None)
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response=(
                        "Thank you! Your order has not been cancelled. Please reach out to our "
                        "Customer Support team at (021) 111-624-333 and they will be glad to check "
                        "what discount or gift card we can offer you. Have a great day!"
                    )
                )

            if choice == "proceed_cancel":
                state.entities.pop("pending_cancel_reason", None)
                cancel_result = self.order_service.execute_cancellation(order_id, reason)
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response=cancel_result.get("message", "Your order has been successfully cancelled.")
                )

            # Unrecognised answer; re-show the two options.
            return FlowResponse(
                status="waiting_for_input",
                response="I didn't quite catch that.\n" + self.CSR_OFFER_OPTIONS
            )

        if state.current_stage == "ask_other_reason":
            custom_reason = (state.conversation_history[-1]["content"] if getattr(state, "conversation_history", None) else "").strip()

            if intent_result.intent == "agent_handoff" or intent_result.escalation_recommended:
                state.current_flow = None
                state.current_stage = None
                state.customer_verified = False
                state.verification_attempts = 0
                return FlowResponse(
                    status="completed",
                    response="I understand. I am connecting you to a customer support representative who can assist you with this request.",
                    tool_request="agent_handoff"
                )

            if not custom_reason:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Could you please tell us the reason for cancelling your order?"
                )

            # Store the customer's own wording as the cancellation reason.
            cancel_result = self.order_service.execute_cancellation(order_id, f"Other: {custom_reason}")
            state.current_flow = None
            state.current_stage = None
            state.customer_verified = False
            state.verification_attempts = 0
            return FlowResponse(
                status="completed",
                response=cancel_result.get("message", "Your order has been successfully cancelled.")
            )

        # Fallback
        state.current_flow = None
        state.current_stage = None
        state.customer_verified = False
        state.verification_attempts = 0
        return FlowResponse(
            status="completed",
            response="I am connecting you to a representative."
        )
