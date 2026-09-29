import re
from typing import Optional
from flows.base import BaseFlow, FlowResponse
from ai.schemas import IntentResult
from core.state_manager import ConversationState
from database.repository import OrderRepository, ComplaintRepository
from datetime import datetime

# --- Category / sub-category taxonomy ---
MISSING_SUBCATEGORIES = ["Missing Item", "Missing Accessories"]
WRONG_SUBCATEGORIES = ["Wrong Product", "Damaged Product", "Expired Product", "Leak product", "Quality Issue"]
REFUND_SUBCATEGORIES = ["Refund", "Warranty Claim", "Cashback", "Change of Mind", "Exchange"]
GENERAL_SUBCATEGORIES = ["Order Info", "Complaint Info", "Extra Parcel", "Delay Delivery", "Duplicate Ticket", "General"]
# Catch-all category used when a customer reports more than one distinct
# complaint (e.g. a missing item AND a wrong item) in the same message, or
# when their description can't be pinned to a single category after a retry.
MISCELLANEOUS_SUBCATEGORIES = ["Miscellaneous"]

# Wrong-category sub-categories that require a photo upload
WRONG_NEEDS_IMAGE = {"Wrong Product", "Damaged Product", "Expired Product", "Leak product", "Quality Issue"}
# Of those, which ones ask a Receive Correct Item / Refund Money resolution question after the photo
WRONG_NEEDS_RESOLUTION = {"Wrong Product", "Damaged Product", "Expired Product", "Leak product", "Quality Issue"}
# Wrong-category sub-categories that just need a text description (no photo) - none currently
WRONG_SIMPLE = set()

# Ordered list of (category, sub_category, label) shown to the customer as a
# numbered menu after verification. The last entry, "Other", has no category:
# the customer types their complaint and it is filed as-is with type "Other".
# Sub-categories not listed here (General, ...) are still reachable by typing
# a description instead of a number.
COMPLAINT_OPTIONS = [
    ("Missing", "Missing Item", "Missing Item"),
    ("Missing", "Missing Accessories", "Missing Accessories"),
    ("Wrong", "Wrong Product", "Wrong Product"),
    ("Wrong", "Damaged Product", "Damaged Product"),
    ("Wrong", "Expired Product", "Expired Product"),
    ("Wrong", "Leak product", "Leaking Product"),
    ("Wrong", "Quality Issue", "Quality Issue"),
    ("Refund", "Refund", "Refund"),
    ("Refund", "Warranty Claim", "Warranty Claim"),
    ("Refund", "Exchange", "Exchange"),
    ("Refund", "Cashback", "Cashback"),
    ("Refund", "Change of Mind", "Change of Mind"),
    ("General", "Delay Delivery", "Delayed Delivery"),
    ("General", "Extra Parcel", "Extra Parcel"),
    ("General", "Order Info", "Order Info"),
    ("General", "Complaint Info", "Complaint Info"),
    ("General", "Duplicate Ticket", "Duplicate Ticket"),
    (None, "Other", "Other"),
]
OTHER_COMPLAINT_TYPE = "Other"


def _complaint_menu_prompt(prefix: str) -> str:
    lines = [prefix, "Please choose your complaint type:"]
    for i, (_, _, label) in enumerate(COMPLAINT_OPTIONS, start=1):
        lines.append(f"{i}. {label}")
    lines.append("\nOr simply describe your issue in your own words.")
    return "\n".join(lines)


def _resolve_complaint_option(user_msg: str):
    """Returns the (category, sub_category, label) picked by a numbered reply
    ("4", "4.", "option 4") or by typing the "Other" label, else None. Only a
    bare number counts, so a description like "2 items missing" is not
    mistaken for option 2."""
    text = (user_msg or "").strip().lower()
    match = re.fullmatch(r"(?:option\s*)?(\d{1,2})[.)]?", text)
    if match:
        idx = int(match.group(1))
        if 1 <= idx <= len(COMPLAINT_OPTIONS):
            return COMPLAINT_OPTIONS[idx - 1]
        return None
    if text == "other":
        return COMPLAINT_OPTIONS[-1]
    return None


def _match_subcategory(msg_lower: str, options) -> Optional[str]:
    for option in options:
        if option.lower() in msg_lower:
            return option
    return None


class ComplaintFlow(BaseFlow):
    def __init__(self, order_repository=None, complaint_repository=None):
        self.order_repository = order_repository or OrderRepository()
        self.complaint_repository = complaint_repository or ComplaintRepository()

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        # Once a complaint is in progress (current_stage is set), every subsequent
        # message is an answer to the current step (order ID, description, image upload,
        # resolution, or details) - never a fresh request.
        return state.current_stage is not None

    def handle(self, intent_result: IntentResult, state: ConversationState) -> FlowResponse:
        # Retrieve raw user message from conversation history
        user_msg = ""
        if state.conversation_history:
            user_msg = state.conversation_history[-1]["content"].strip()

        current_stage = state.current_stage
        entities = intent_result.entities if isinstance(intent_result.entities, dict) else intent_result.entities.model_dump()

        # 1. Trigger / Verify Order ID
        if current_stage is None:
            # Cache the initial complaint message so we don't ask for it again later
            if "initial_complaint_description" not in state.entities:
                state.entities["initial_complaint_description"] = user_msg
                
            order_id = state.entities.get("order_id")
            if order_id is not None and not isinstance(order_id, str):
                order_id = str(order_id)
            if not order_id:
                order_id = None

            if not order_id and intent_result.entities:
                extracted = entities.get("order_id")
                if extracted:
                    order_id = str(extracted)

            if not order_id:
                # Use regex to find order ID in user message as fallback
                match = re.search(r'\b(?=[a-zA-Z0-9-]*\d)[a-zA-Z0-9-]{5,20}\b', user_msg)
                if match:
                    order_id = match.group(0)

            if not order_id:
                return FlowResponse(
                    status="waiting_for_input",
                    response="I can register your complaint. What is your Order ID?",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_order_id",
                        "waiting_for_order_id": True
                    }
                )
            else:
                return self._verify_order(state, order_id)

        # 2. Waiting for Order ID stage
        if current_stage == "waiting_for_order_id":
            order_id = None
            extracted = entities.get("order_id")
            if extracted:
                order_id = str(extracted)

            if not order_id:
                match = re.search(r'\b(?=[a-zA-Z0-9-]*\d)[a-zA-Z0-9-]{5,20}\b', user_msg)
                if match:
                    order_id = match.group(0)

            if not order_id:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please provide a valid Order ID to proceed.",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_order_id",
                        "waiting_for_order_id": True
                    }
                )
            else:
                return self._verify_order(state, order_id)

        # 2.5 Waiting for Phone verification stage
        if current_stage == "waiting_for_phone":
            order_id = state.entities.get("order_id")
            from services.order_service import OrderService
            order_service = OrderService(repository=self.order_repository)
            if order_service.verify_customer(order_id, user_msg):
                state.customer_verified = True
                state.verification_attempts = 0
                
                # Check delivery date for 7-day return policy
                resolved_id = self.order_repository.resolve_to_latest_order_id(order_id)
                delivery_date = self.order_repository.get_delivery_date(resolved_id)
                if delivery_date and isinstance(delivery_date, datetime):
                    days_passed = (datetime.now() - delivery_date).days
                    if days_passed >= 7:
                        state.current_flow = None
                        state.current_stage = None
                        return FlowResponse(
                            status="completed",
                            response="We are sorry, but more than 7 days have passed since the delivery of your order. According to Naheed's return policy, the product cannot be returned. We apologize for any inconvenience.",
                        )
                
                # Verification successful, transition to description collection or skip if known
                category = state.entities.get("complaint_category")
                sub_category = state.entities.get("complaint_sub_category")
                if category and sub_category:
                    return self._transition_to_subcategory(
                        state,
                        category,
                        sub_category,
                        state.entities.get("initial_complaint_description", "User provided description earlier.")
                    )
                else:
                    return FlowResponse(
                        status="waiting_for_input",
                        response=_complaint_menu_prompt("Verification successful."),
                        updated_state={
                            "current_flow": "complaint",
                            "current_stage": "waiting_for_complaint_option"
                        }
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

        # 2.8 Waiting for a numbered complaint-type selection
        if current_stage == "waiting_for_complaint_option":
            option = _resolve_complaint_option(user_msg)
            if option:
                category, sub_category, _ = option
                if sub_category == OTHER_COMPLAINT_TYPE:
                    return FlowResponse(
                        status="waiting_for_input",
                        response="Please type your complaint in your own words and we will register it.",
                        updated_state={
                            "current_flow": "complaint",
                            "current_stage": "waiting_for_other_complaint"
                        }
                    )
                # Picked from the menu, so there is no typed description yet
                return self._transition_to_subcategory(state, category, sub_category, "")
            # Free text instead of a number: categorize it like a description
            current_stage = "waiting_for_complaint_description"

        # 2.9 Waiting for a manually typed "Other" complaint
        if current_stage == "waiting_for_other_complaint":
            if not user_msg:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please type your complaint in your own words and we will register it."
                )
            return FlowResponse(
                status="completed",
                response="",
                updated_state={"current_flow": None, "current_stage": None},
                tool_request="create_complaint",
                tool_args={
                    "order_id": state.entities.get("order_id"),
                    "complaint_type": OTHER_COMPLAINT_TYPE,
                    "details": f"Sub-category: {OTHER_COMPLAINT_TYPE}. Details: {user_msg}",
                    "image_url": None
                }
            )

        # 3. Waiting for Complaint Description (AI/LLM-based categorization)
        if current_stage == "waiting_for_complaint_description":
            # Extract category & subcategory from entities (LLM output)
            category = entities.get("complaint_category") or state.entities.get("complaint_category")
            sub_category = entities.get("complaint_sub_category") or state.entities.get("complaint_sub_category")

            # Fallback keyword matching if LLM didn't resolve them
            if not category or not sub_category:
                msg_lower = user_msg.lower()

                # First, detect how many DISTINCT categories are referenced in this
                # single message. If the customer describes more than one complaint
                # at once (e.g. "item is missing and the other one is damaged"),
                # route the whole thing to the Miscellaneous catch-all category
                # instead of forcing it into just one bucket.
                detected_categories = set()
                for cat, sub_list in [
                    ("Missing", MISSING_SUBCATEGORIES),
                    ("Wrong", WRONG_SUBCATEGORIES),
                    ("Refund", REFUND_SUBCATEGORIES),
                    ("General", GENERAL_SUBCATEGORIES)
                ]:
                    if _match_subcategory(msg_lower, sub_list):
                        detected_categories.add(cat)

                if "missing" in msg_lower or "khoya" in msg_lower or "nahi mila" in msg_lower or "kam" in msg_lower:
                    detected_categories.add("Missing")
                if "wrong" in msg_lower or "kharab" in msg_lower or "expired" in msg_lower or "leak" in msg_lower or "damaged" in msg_lower or "broken" in msg_lower or "quality" in msg_lower:
                    detected_categories.add("Wrong")
                if "refund" in msg_lower or "paisa" in msg_lower or "cashback" in msg_lower or "warranty" in msg_lower:
                    detected_categories.add("Refund")
                if "delay" in msg_lower or "delivery" in msg_lower or "info" in msg_lower or "packet" in msg_lower or "extra" in msg_lower or "duplicate" in msg_lower:
                    detected_categories.add("General")

                if len(detected_categories) > 1:
                    category = "Miscellaneous"
                    sub_category = "Miscellaneous"
                else:
                    # Check subcategories first
                    for cat, sub_list in [
                        ("Missing", MISSING_SUBCATEGORIES),
                        ("Wrong", WRONG_SUBCATEGORIES),
                        ("Refund", REFUND_SUBCATEGORIES),
                        ("General", GENERAL_SUBCATEGORIES)
                    ]:
                        matched = _match_subcategory(msg_lower, sub_list)
                        if matched:
                            category = cat
                            sub_category = matched
                            break

                    # If no subcategory matched, check category keywords
                    if not category:
                        if "missing" in msg_lower or "khoya" in msg_lower or "nahi mila" in msg_lower or "kam" in msg_lower:
                            category = "Missing"
                            sub_category = "Missing Item" # Default subcategory
                        elif "wrong" in msg_lower or "kharab" in msg_lower or "expired" in msg_lower or "leak" in msg_lower or "damaged" in msg_lower or "quality" in msg_lower:
                            category = "Wrong"
                            if "quality" in msg_lower:
                                sub_category = "Quality Issue"
                            elif "leak" in msg_lower:
                                sub_category = "Leak product"
                            elif "expired" in msg_lower:
                                sub_category = "Expired Product"
                            elif "damaged" in msg_lower or "broken" in msg_lower or "kharab" in msg_lower:
                                sub_category = "Damaged Product"
                            else:
                                sub_category = "Wrong Product"
                        elif "refund" in msg_lower or "paisa" in msg_lower or "cashback" in msg_lower or "warranty" in msg_lower or "exchange" in msg_lower or "replace" in msg_lower:
                            category = "Refund"
                            if "warranty" in msg_lower:
                                sub_category = "Warranty Claim"
                            elif "cashback" in msg_lower:
                                sub_category = "Cashback"
                            elif "change of mind" in msg_lower:
                                sub_category = "Change of Mind"
                            elif "exchange" in msg_lower or "replace" in msg_lower:
                                sub_category = "Exchange"
                            else:
                                sub_category = "Refund"
                        elif "delay" in msg_lower or "delivery" in msg_lower or "info" in msg_lower or "packet" in msg_lower or "duplicate" in msg_lower:
                            category = "General"
                            if "duplicate" in msg_lower:
                                sub_category = "Duplicate Ticket"
                            elif "delay" in msg_lower:
                                sub_category = "Delay Delivery"
                            elif "extra" in msg_lower:
                                sub_category = "Extra Parcel"
                            else:
                                sub_category = "General"

            # If still not classified, ask the user to clarify - but only once.
            # A prior version of this flow could re-ask indefinitely if the
            # customer's wording never matched any keyword list (most often
            # when they were describing more than one issue in an unusual
            # phrasing). Bound the retries: after one failed clarification
            # attempt, stop asking and register it as a Miscellaneous
            # complaint using their own words, so the user is never trapped.
            if not category or not sub_category:
                attempts = int(state.entities.get("complaint_desc_attempts", 0)) + 1
                if attempts >= 2:
                    order_id = state.entities.get("order_id")
                    details = f"Sub-category: Miscellaneous. Details: {user_msg}"
                    return FlowResponse(
                        status="completed",
                        response="",
                        updated_state={
                            "current_flow": None,
                            "current_stage": None,
                            "entities": {
                                **state.entities,
                                "complaint_category": "Miscellaneous",
                                "complaint_sub_category": "Miscellaneous",
                                "complaint_desc_attempts": 0
                            }
                        },
                        tool_request="create_complaint",
                        tool_args={
                            "order_id": order_id,
                            "complaint_type": "Miscellaneous",
                            "details": details,
                            "image_url": None
                        }
                    )
                return FlowResponse(
                    status="waiting_for_input",
                    response=_complaint_menu_prompt("Sorry, I couldn't identify the issue."),
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_complaint_option",
                        "entities": {**state.entities, "complaint_desc_attempts": attempts}
                    }
                )

            # Direct transition to the matched subcategory handling
            return self._transition_to_subcategory(state, category, sub_category, user_msg)

        # Legacy fallback handlers for backward compatibility (e.g. for existing unit tests)
        if current_stage == "waiting_for_category":
            msg_lower = user_msg.lower()
            if "missing" in msg_lower:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please select the sub-category:\n- Missing Item\n- Missing Accessories",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_missing_sub_category",
                        "entities": {**state.entities, "complaint_category": "Missing"}
                    }
                )
            elif "wrong" in msg_lower:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please select the sub-category:\n- Wrong Product\n- Damaged Product\n- Expired Product\n- Leak product\n- Quality Issue",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_wrong_sub_category",
                        "entities": {**state.entities, "complaint_category": "Wrong"}
                    }
                )
            elif "refund" in msg_lower or "exchange" in msg_lower or "replace" in msg_lower:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please select the sub-category:\n- Refund\n- Warranty Claim\n- Cashback\n- Change of Mind",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_refund_sub_category",
                        "entities": {**state.entities, "complaint_category": "Refund"}
                    }
                )
            elif "general" in msg_lower:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please select the sub-category:\n- Order Info\n- Complaint Info\n- Extra Parcel\n- Delay Delivery\n- Duplicate Ticket\n- General",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_general_sub_category",
                        "entities": {**state.entities, "complaint_category": "General"}
                    }
                )

        if current_stage == "waiting_for_missing_sub_category":
            msg_lower = user_msg.lower()
            sub_cat = _match_subcategory(msg_lower, MISSING_SUBCATEGORIES)
            if sub_cat:
                return self._transition_to_subcategory(state, "Missing", sub_cat, user_msg)

        if current_stage == "waiting_for_wrong_sub_category":
            msg_lower = user_msg.lower()
            sub_cat = _match_subcategory(msg_lower, WRONG_SUBCATEGORIES)
            if sub_cat:
                return self._transition_to_subcategory(state, "Wrong", sub_cat, user_msg)

        if current_stage == "waiting_for_refund_sub_category":
            msg_lower = user_msg.lower()
            sub_cat = _match_subcategory(msg_lower, REFUND_SUBCATEGORIES)
            if sub_cat:
                return self._transition_to_subcategory(state, "Refund", sub_cat, user_msg)

        if current_stage == "waiting_for_general_sub_category":
            msg_lower = user_msg.lower()
            if "order info" in msg_lower:
                return self._transition_to_subcategory(state, "General", "Order Info", user_msg)
            elif "complaint info" in msg_lower:
                return self._transition_to_subcategory(state, "General", "Complaint Info", user_msg)
            elif "extra parcel" in msg_lower:
                return self._transition_to_subcategory(state, "General", "Extra Parcel", user_msg)
            elif "delay delivery" in msg_lower:
                return self._transition_to_subcategory(state, "General", "Delay Delivery", user_msg)
            elif "duplicate ticket" in msg_lower:
                return self._transition_to_subcategory(state, "General", "Duplicate Ticket", user_msg)
            elif "general" in msg_lower:
                return self._transition_to_subcategory(state, "General", "General", user_msg)

        # 4. Waiting for Image Upload (Wrong category sub-categories that need a photo)
        if current_stage == "waiting_for_image":
            # Try to extract an image URL. Support both bracketed format and plain 'image received' phrase.
            image_match = re.search(r'\[Image Uploaded:\s*([^\]]+)\]|\bimage received\b', user_msg, re.IGNORECASE)
            sub_cat = state.entities.get("complaint_sub_category", "Wrong Product")
            if image_match:
                # If the bracketed format matched, extract the URL; otherwise use a placeholder.
                image_path = image_match.group(1).strip() if image_match.group(1) else 'uploaded_image_placeholder'

                order_id = state.entities.get("order_id")
                description = state.entities.get("complaint_description")
                if description is None:
                    description = state.entities.get("initial_complaint_description", "")
                details = f"Sub-category: {sub_cat}." + (f" Details: {description}" if description else "")
                return FlowResponse(
                    status="completed",
                    response="",
                    updated_state={
                        "current_flow": None,
                        "current_stage": None,
                        "show_upload": False
                    },
                    tool_request="create_complaint",
                    tool_args={
                        "order_id": order_id,
                        "complaint_type": sub_cat,
                        "details": details,
                        "image_url": image_path
                    }
                )
            else:
                # Text instead of a photo (common after picking the type from the
                # menu): keep it as the complaint description, then ask again.
                updated_state = {
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_image",
                    "show_upload": True
                }
                prompt = f"Please upload an image related to your {sub_cat.lower()} complaint to proceed."
                if user_msg:
                    updated_state["entities"] = {**state.entities, "complaint_description": user_msg}
                    prompt = f"Thank you, noted. {prompt}"
                return FlowResponse(
                    status="waiting_for_input",
                    response=prompt,
                    updated_state=updated_state
                )

        # 5. Waiting for Resolution
        if current_stage == "waiting_for_resolution":
            msg_lower = user_msg.lower()

            # Check if they want to switch to a different issue
            if "different" in msg_lower or "other" in msg_lower or "something else" in msg_lower:
                return FlowResponse(
                    status="waiting_for_input",
                    response=_complaint_menu_prompt("No problem."),
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_complaint_option",
                        "entities": {
                            **state.entities,
                            "complaint_category": None,
                            "complaint_sub_category": None,
                            "unavailable_items": None,
                            "complaint_desc_attempts": 0
                        }
                    }
                )

            resolution = None
            sub_cat = state.entities.get("complaint_sub_category") or "Wrong Product"

            if "refund" in msg_lower or "money" in msg_lower:
                resolution = "Refund Money"
            elif "correct" in msg_lower or "receive" in msg_lower or "replace" in msg_lower:
                resolution = "Receive Correct Item"

            if not resolution:
                opts = "Please choose between 'Receive Correct Item' or 'Refund Money'."
                return FlowResponse(
                    status="waiting_for_input",
                    response=f"Invalid option. {opts}",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_resolution"
                    }
                )

            # All details collected! Complete ticket registration.
            order_id = state.entities.get("order_id")
            image_url = state.entities.get("image_url")
            description = state.entities.get("complaint_description") or state.entities.get("initial_complaint_description", "")
            details = f"Sub-category: {sub_cat}. Resolution: {resolution}. Details: {description}".strip()

            return FlowResponse(
                status="completed",
                response="",
                updated_state={
                    "current_flow": None,
                    "current_stage": None,
                    "show_upload": False
                },
                tool_request="create_complaint",
                tool_args={
                    "order_id": order_id,
                    "complaint_type": sub_cat,
                    "details": details,
                    "image_url": image_url
                }
            )

        # 6. Waiting for Missing Item Details
        if current_stage == "waiting_for_missing_details":
            order_id = state.entities.get("order_id")
            dispatch_missing = state.entities.get("dispatch_missing_items") or []
            is_cod = state.entities.get("is_cod", False)
            refund_status = state.entities.get("refund_status")
            additional = user_msg.strip()
            no_additional = additional.lower() in ["no", "none", "nope", "n", "no.", "nahi", "nahi."]

            detail_parts = ["Sub-category: Missing Item."]
            if dispatch_missing:
                detail_parts.append(f"Items missing at dispatch: {', '.join(dispatch_missing)}.")
                if is_cod:
                    detail_parts.append("Payment: COD (not charged for dispatch-missing items).")
                else:
                    detail_parts.append(f"Refund status for dispatch-missing items: {refund_status or 'not yet initiated'}.")
            if not no_additional:
                detail_parts.append(f"Additional items not received by customer: {additional}.")

            if no_additional and not dispatch_missing:
                return FlowResponse(
                    status="completed",
                    response="Thank you. No missing items were reported. If you need further help, feel free to reach out.",
                    updated_state={"current_flow": None, "current_stage": None}
                )

            if no_additional and is_cod and dispatch_missing:
                return FlowResponse(
                    status="completed",
                    response="Thank you. Since this was a COD order, no refund is applicable for the missing dispatch items. No complaint has been filed.",
                    updated_state={"current_flow": None, "current_stage": None}
                )

            return FlowResponse(
                status="completed",
                response="",
                updated_state={"current_flow": None, "current_stage": None, "show_upload": False},
                tool_request="create_complaint",
                tool_args={
                    "order_id": order_id,
                    "complaint_type": "Missing Item",
                    "details": " ".join(detail_parts),
                    "image_url": None
                }
            )

        # 7. Waiting for Simple Text Details
        if current_stage == "waiting_for_simple_details":
            order_id = state.entities.get("order_id")
            sub_cat = state.entities.get("complaint_sub_category", "General")
            details = f"Sub-category: {sub_cat}. Details: {user_msg}"

            return FlowResponse(
                status="completed",
                response="",
                updated_state={
                    "current_flow": None,
                    "current_stage": None
                },
                tool_request="create_complaint",
                tool_args={
                    "order_id": order_id,
                    "complaint_type": sub_cat,
                    "details": details,
                    "image_url": None
                }
            )

        # 8. Waiting for General Details
        if current_stage == "waiting_for_general_details":
            order_id = state.entities.get("order_id")
            details = user_msg

            return FlowResponse(
                status="completed",
                response="",
                updated_state={
                    "current_flow": None,
                    "current_stage": None
                },
                tool_request="create_complaint",
                tool_args={
                    "order_id": order_id,
                    "complaint_type": "General",
                    "details": details,
                    "image_url": None
                }
            )

        return FlowResponse(
            status="completed",
            response="I'm sorry, we encountered an error handling your complaint.",
            updated_state={"current_flow": None, "current_stage": None}
        )

    def _verify_order(self, state: ConversationState, order_id: str) -> FlowResponse:
        # Resolve/look up the order, then either ask for phone verification or,
        # for an already-verified customer, move straight on to the complaint.
        try:
            resolved_id = self.order_repository.resolve_to_latest_order_id(order_id)
            self.order_repository.get_order_by_increment_id(resolved_id)
            new_entities = {**state.entities, "order_id": resolved_id}
            if resolved_id != order_id:
                new_entities["parent_order_id"] = order_id

            if getattr(state, "customer_verified", False):
                # Check delivery date for 7-day return policy
                delivery_date = self.order_repository.get_delivery_date(resolved_id)
                if delivery_date and isinstance(delivery_date, datetime):
                    days_passed = (datetime.now() - delivery_date).days
                    if days_passed >= 7:
                        return FlowResponse(
                            status="completed",
                            response="We are sorry, but more than 7 days have passed since the delivery of your order. According to Naheed's return policy, the product cannot be returned. We apologize for any inconvenience.",
                            updated_state={"current_flow": None, "current_stage": None}
                        )
                # Direct to description collection instead of category menu
                category = new_entities.get("complaint_category")
                sub_category = new_entities.get("complaint_sub_category")
                if category and sub_category:
                    state.entities = new_entities
                    return self._transition_to_subcategory(
                        state,
                        category,
                        sub_category,
                        new_entities.get("initial_complaint_description", "User provided description earlier.")
                    )
                else:
                    return FlowResponse(
                        status="waiting_for_input",
                        response=_complaint_menu_prompt("Order ID verified."),
                        updated_state={
                            "current_flow": "complaint",
                            "current_stage": "waiting_for_complaint_option",
                            "waiting_for_order_id": False,
                            "entities": new_entities
                        }
                    )
            else:
                return FlowResponse(
                    status="waiting_for_input",
                    response="For security purposes, please provide the phone number associated with this order.",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_phone",
                        "waiting_for_order_id": False,
                        "entities": new_entities
                    }
                )
        except Exception:
            return FlowResponse(
                status="waiting_for_input",
                response="We couldn't find an order with that ID. Please check and try again.",
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_order_id",
                    "waiting_for_order_id": True
                }
            )

    def _transition_to_subcategory(self, state: ConversationState, category: str, sub_category: str, user_description: str) -> FlowResponse:
        order_id = state.entities.get("order_id", "")
        parent_order_id = state.entities.get("parent_order_id", "")
        new_entities = {
            **state.entities,
            "complaint_category": category,
            "complaint_sub_category": sub_category,
            # The message that actually described the issue - used for the ticket
            # details instead of the first message (which may be just a menu digit
            # or an order ID).
            "complaint_description": user_description
        }

        # Reject a duplicate complaint type up front, before asking the customer
        # for any more details or a photo. The complaint_type filed later is
        # always the sub-category (e.g. "Missing Item", "Damaged Product", "General").
        try:
            if self.complaint_repository.has_existing_complaint_type(order_id, sub_category):
                return FlowResponse(
                    status="completed",
                    response=f"You have already filed a {sub_category} complaint for Order #{order_id}. Duplicate complaints of the same type are not allowed.",
                    updated_state={"current_flow": None, "current_stage": None}
                )
        except Exception:
            # ComplaintService re-checks before inserting, so a lookup failure
            # here shouldn't block the customer.
            pass

        if category == "Missing" and sub_category == "Missing Item":
            dispatch_missing = []
            if parent_order_id and parent_order_id != order_id:
                try:
                    dispatch_missing = self.order_repository.get_unavailable_items(parent_order_id, order_id)
                except Exception:
                    dispatch_missing = []

            payment_method = self.order_repository.get_payment_method(order_id)
            is_cod = payment_method.lower() == "cashondelivery"

            lines = []
            refund_status = None
            if dispatch_missing:
                items_str = "\n• " + "\n• ".join(dispatch_missing)
                lines.append(f"We found the following items were **missing at dispatch**:{items_str}")
                if is_cod:
                    lines.append("Since your order was placed with **Cash on Delivery**, you were not charged for these items — no refund is applicable for them.")
                else:
                    refund_status = self.order_repository.get_refund_status(order_id)
                    if refund_status == "completed":
                        lines.append("A refund for these items has already been **processed**. Please allow 3–5 business days for it to reflect.")
                    elif refund_status == "in progress":
                        lines.append("A refund for these items is currently **in progress** and will be processed shortly.")
                    else:
                        lines.append("Our customer service team will **initiate a refund** for these items.")
                lines.append("Were there any **other items** (besides the above) that you did not receive? If yes, please describe them. If no, type 'No'.")
            else:
                lines.append("We have no record of items missing at dispatch for your order.")
                lines.append("Please describe which items you did **not receive**.")

            return FlowResponse(
                status="waiting_for_input",
                response="\n\n".join(lines),
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_missing_details",
                    "entities": {
                        **new_entities,
                        "dispatch_missing_items": dispatch_missing,
                        "is_cod": is_cod,
                        "refund_status": refund_status if (not is_cod and dispatch_missing) else None
                    }
                }
            )

        elif sub_category in WRONG_NEEDS_IMAGE:
            return FlowResponse(
                status="waiting_for_input",
                response=f"I have registered this as a {sub_category.lower()} complaint. Please upload an image related to your complaint (you can also type a short description).",
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_image",
                    "show_upload": True,
                    "entities": new_entities
                }
            )

        elif category == "General" and sub_category == "General":
            return FlowResponse(
                status="waiting_for_input",
                response="Please describe the issue you are experiencing.",
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_general_details",
                    "entities": new_entities
                }
            )

        else:
            return FlowResponse(
                status="waiting_for_input",
                response=f"Please describe your {sub_category.lower()} issue in more detail.",
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_simple_details",
                    "entities": new_entities
                }
            )
