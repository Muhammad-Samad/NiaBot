import json
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
REFUND_SUBCATEGORIES = ["Warranty Claim"]
GENERAL_SUBCATEGORIES = ["Extra Parcel", "Delay Delivery", "General"]
# Catch-all category used when a customer reports more than one distinct
# complaint (e.g. a missing item AND a wrong item) in the same message, or
# when their description can't be pinned to a single category after a retry.
MISCELLANEOUS_SUBCATEGORIES = ["Miscellaneous"]

# Allowed sub-categories per category - used to reject an LLM-extracted
# category/sub-category that isn't (or is no longer) a valid complaint type.
CATEGORY_SUBCATEGORIES = {
    "Missing": MISSING_SUBCATEGORIES,
    "Wrong": WRONG_SUBCATEGORIES,
    "Refund": REFUND_SUBCATEGORIES,
    "General": GENERAL_SUBCATEGORIES,
    "Miscellaneous": MISCELLANEOUS_SUBCATEGORIES,
}

# Wrong-category sub-categories that require a photo upload
WRONG_NEEDS_IMAGE = {"Wrong Product", "Damaged Product", "Expired Product", "Leak product", "Quality Issue", "Missing Accessories", "Warranty Claim", "Extra Parcel", "Other"}
# Sub-categories filed without asking the customer which order items the
# complaint is about. Every other type shows the order's items to pick from,
# and the picked ones are saved to nhd_complain_tickets_products_info.
ITEMS_NOT_REQUIRED_CATEGORIES = {"Delay Delivery", "Extra Parcel"}
# Of those, which ones ask a Receive Correct Item / Refund Money resolution question after the photo
WRONG_NEEDS_RESOLUTION = {"Wrong Product", "Damaged Product", "Expired Product", "Leak product", "Quality Issue","Missing Accessories", "Warranty Claim", "Extra Parcel", "Other"}
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
    ("Refund", "Warranty Claim", "Warranty Claim"),
    ("General", "Delay Delivery", "Delayed Delivery"),
    ("General", "Extra Parcel", "Extra Parcel"),
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


# Prefix of the last line of the item-selection prompt: the items as JSON, which
# the frontend renders as a product table (image, price, ordered/complaint qty)
# in place of the plain numbered list.
ITEM_PICKER_MARKER = "ITEM_PICKER::"


def _item_selection_prompt(items: list, prefix: str) -> str:
    # "one or more" tells the frontend to render this list as a multi-select.
    lines = [prefix, "Please select the items you have a complaint about (you can select one or more):"]
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item['name']} (Qty: {item['qty']:g})")
    lines.append("\nReply with the item numbers and complaint quantities (e.g. 1:2, 3 - the quantity is 1 if not given), or type 'all'.")
    picker = [
        {"n": i, "name": item["name"], "image_url": item.get("image_url"),
         "price": item.get("price") or 0, "qty": item.get("qty") or 0}
        for i, item in enumerate(items, start=1)
    ]
    lines.append(ITEM_PICKER_MARKER + json.dumps(picker))
    return "\n".join(lines)


# Prefix of the last line of an image-upload prompt: {"optional": bool} as JSON.
# The frontend renders it as an upload panel (multiple images, thumbnails, and
# a Skip button when optional) - there is no image upload anywhere else.
IMAGE_UPLOAD_MARKER = "IMAGE_UPLOAD::"
# What the upload panel sends back: "[Images Uploaded: <url>, <url>]" (the older
# single "[Image Uploaded: <url>]" form is still accepted), or "[Image Skipped]".
IMAGES_UPLOADED_RE = re.compile(r"\[Images? Uploaded:\s*([^\]]+)\]", re.IGNORECASE)
SKIP_WORDS = {"skip", "[image skipped]", "no", "none", "nope", "n", "nahi"}


def _image_upload_prompt(text: str, optional: bool = False) -> str:
    return f"{text}\n{IMAGE_UPLOAD_MARKER}{json.dumps({'optional': optional})}"


# Last line of a prompt for an optional answer: the frontend shows a Skip button
# under it that replies "skip".
SKIP_OPTION_MARKER = "SKIP_OPTION::"


def _skippable_prompt(text: str) -> str:
    return f"{text}\n{SKIP_OPTION_MARKER}"


def _parse_uploaded_images(user_msg: str) -> list:
    match = IMAGES_UPLOADED_RE.search(user_msg or "")
    if not match:
        return []
    return [url.strip() for url in match.group(1).split(",") if url.strip()]


def _parse_item_selection(user_msg: str, items: list) -> Optional[list]:
    """Returns (0-based index, complaint qty) pairs picked by a reply like "2",
    "1, 3", "1 and 3", "1:2, 3:1" or "all" (qty defaults to 1), else None -
    also when a number is out of range or a qty exceeds the ordered qty."""
    count = len(items)
    text = (user_msg or "").strip().lower()
    if text in {"all", "all items", "sab", "sab items"}:
        return [(i, 1) for i in range(count)]
    text = re.sub(r"\band\b|&", ",", text)
    pair = r"\d{1,3}(?:\s*[:x×]\s*\d{1,4})?"
    if not re.fullmatch(rf"\s*{pair}(?:[\s,]+{pair})*[\s,]*", text):
        return None
    picked = {}
    for n, q in re.findall(r"(\d{1,3})(?:\s*[:x×]\s*(\d{1,4}))?", text):
        idx = int(n) - 1
        if not 0 <= idx < count:
            return None
        qty = int(q) if q else 1
        ordered = float(items[idx].get("qty") or 0)
        if qty < 1 or (ordered >= 1 and qty > ordered):
            return None
        picked[idx] = qty
    return list(picked.items())


def _match_subcategory(msg_lower: str, options) -> Optional[str]:
    for option in options:
        if option.lower() in msg_lower:
            return option
    return None


class ComplaintFlow(BaseFlow):
    def __init__(self, order_repository=None, complaint_repository=None):
        self.order_repository = order_repository or OrderRepository()
        self.complaint_repository = complaint_repository or ComplaintRepository()

    # Stages where the complaint hasn't really started yet - it's still asking
    # who the customer is. A clear request for another operations flow here
    # ("track my order" while being asked for the order ID) means the customer
    # changed their mind, so the complaint gives way to it.
    _EARLY_STAGES = {"waiting_for_order_id", "waiting_for_phone"}
    _SWITCH_INTENTS = {"order_tracking", "cancel_order", "complaint_tracking", "modify_order", "refund"}

    def is_continuation(self, intent_result: IntentResult, state: ConversationState) -> bool:
        if state.current_stage is None:
            return False
        # An explicit request for a human always wins, at any step.
        if intent_result.intent == "agent_handoff":
            return False
        if state.current_stage in self._EARLY_STAGES and intent_result.intent in self._SWITCH_INTENTS:
            # ...unless the message is the order ID / phone number being asked
            # for (the rule-based fallback router labels a bare order ID as
            # order_tracking).
            user_msg = state.conversation_history[-1]["content"] if state.conversation_history else ""
            return bool(re.search(r"\d{5,}", re.sub(r"[\s\-]", "", user_msg)))
        # From the complaint menu onwards every message is an answer to the
        # current step (complaint type, description, image upload, resolution)
        # - descriptions naturally mention "late", "refund", "my order", etc.
        return True

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
                            response="I’m sorry, but more than 7 days have passed since your order was delivered. According to Naheed’s policy, we’re unable to register a complaint after this period. You can contact our Customer Support team at (021) 111-624-333 for further assistance.",
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
                        response="I am unable to verify the provided phone number. Please contact our Customer Support team at (021) 111-624-333 for further assistance.",
                        tool_request="agent_handoff"
                    )
                else:
                    return FlowResponse(
                        status="waiting_for_input",
                        # response="The phone number provided does not match our records. Please try again."
                        response="I am unable to verify the provided phone number. Please contact our Customer Support team at (021) 111-624-333 for further assistance."
                    )

        # 2.8 Waiting for a numbered complaint-type selection
        if current_stage == "waiting_for_complaint_option":
            option = _resolve_complaint_option(user_msg)
            if option:
                category, sub_category, _ = option
                if sub_category == OTHER_COMPLAINT_TYPE:
                    # "Other" may be filed more than once per order, so it skips
                    # the duplicate check in _transition_to_subcategory.
                    new_entities = {
                        **state.entities,
                        "complaint_category": None,
                        "complaint_sub_category": OTHER_COMPLAINT_TYPE,
                        "complaint_items": []
                    }
                    return self._ask_for_items(new_entities, "Sure.")
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
            # Photos are optional for "Other" - offer the upload panel with a Skip.
            return FlowResponse(
                status="waiting_for_input",
                response=_image_upload_prompt(
                    "Thank you, noted. If you have any pictures related to your complaint, please upload them. Otherwise, you can skip this step.",
                    optional=True
                ),
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_image",
                    "entities": {**state.entities, "complaint_description": user_msg}
                }
            )

        # 3. Waiting for Complaint Description (AI/LLM-based categorization)
        if current_stage == "waiting_for_complaint_description":
            # Extract category & subcategory from entities (LLM output)
            category = entities.get("complaint_category") or state.entities.get("complaint_category")
            sub_category = entities.get("complaint_sub_category") or state.entities.get("complaint_sub_category")
            if sub_category not in CATEGORY_SUBCATEGORIES.get(category, []):
                # Not a valid complaint type (e.g. a removed one like "Refund" or
                # "Cashback") - fall back to keyword matching below.
                category = None
                sub_category = None

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
                if "warranty" in msg_lower:
                    detected_categories.add("Refund")
                if "delay" in msg_lower or "delivery" in msg_lower or "packet" in msg_lower or "extra" in msg_lower:
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
                        elif "warranty" in msg_lower:
                            category = "Refund"
                            sub_category = "Warranty Claim"
                        elif "delay" in msg_lower or "delivery" in msg_lower or "packet" in msg_lower or "extra" in msg_lower:
                            category = "General"
                            if "delay" in msg_lower:
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
                            "image_urls": []
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
            elif "refund" in msg_lower or "warranty" in msg_lower:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please select the sub-category:\n- Warranty Claim",
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_refund_sub_category",
                        "entities": {**state.entities, "complaint_category": "Refund"}
                    }
                )
            elif "general" in msg_lower:
                return FlowResponse(
                    status="waiting_for_input",
                    response="Please select the sub-category:\n- Extra Parcel\n- Delay Delivery\n- General",
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
            if "extra parcel" in msg_lower:
                return self._transition_to_subcategory(state, "General", "Extra Parcel", user_msg)
            elif "delay delivery" in msg_lower:
                return self._transition_to_subcategory(state, "General", "Delay Delivery", user_msg)
            elif "general" in msg_lower:
                return self._transition_to_subcategory(state, "General", "General", user_msg)

        # 3.5 Waiting for the customer to pick the order items the complaint is about
        if current_stage == "waiting_for_item_selection":
            items = state.entities.get("complaint_order_items") or []
            picked = _parse_item_selection(user_msg, items)
            if not picked:
                return FlowResponse(
                    status="waiting_for_input",
                    response=_item_selection_prompt(items, "Sorry, I couldn't understand your selection.")
                )
            new_entities = {
                **state.entities,
                "complaint_items": [{**items[i], "complaint_qty": qty} for i, qty in picked]
            }
            return self._ask_complaint_details(new_entities)

        # 4. Waiting for Image Upload (one or more photos; optional for "Other")
        if current_stage == "waiting_for_image":
            sub_cat = state.entities.get("complaint_sub_category", "Wrong Product")
            optional = sub_cat == OTHER_COMPLAINT_TYPE
            image_urls = _parse_uploaded_images(user_msg)
            if not image_urls and re.search(r"\bimage received\b", user_msg, re.IGNORECASE):
                image_urls = ["uploaded_image_placeholder"]
            skipped = optional and user_msg.lower() in SKIP_WORDS
            if image_urls or skipped:
                if state.entities.get("complaint_description"):
                    return self._file_image_complaint(state.entities, image_urls)
                # Type picked from the menu, so nothing describes the issue yet:
                # ask for a description as a separate, optional step.
                return FlowResponse(
                    status="waiting_for_input",
                    response=_skippable_prompt(
                        "Thank you. Would you like to add a short description of the issue? Type it below, or skip this step."
                    ),
                    updated_state={
                        "current_flow": "complaint",
                        "current_stage": "waiting_for_image_description",
                        "entities": {**state.entities, "image_urls": image_urls}
                    }
                )
            # Text instead of a photo: keep it as the complaint description
            # (added to what "Other" already typed), then ask again.
            updated_state = {"current_flow": "complaint", "current_stage": "waiting_for_image"}
            if optional:
                prompt = "Please upload pictures related to your complaint, or skip this step."
            else:
                prompt = f"Please upload an image related to your {sub_cat.lower()} complaint to proceed."
            if user_msg.lower() in SKIP_WORDS:
                prompt = f"An image is required for a {sub_cat.lower()} complaint. {prompt}"
            elif user_msg:
                description = user_msg
                if optional and state.entities.get("complaint_description"):
                    description = f"{state.entities['complaint_description']} {user_msg}"
                updated_state["entities"] = {**state.entities, "complaint_description": description}
                prompt = f"Thank you, noted. {prompt}"
            return FlowResponse(
                status="waiting_for_input",
                response=_image_upload_prompt(prompt, optional=optional),
                updated_state=updated_state
            )

        # 4.5 Optional description after the images were uploaded
        if current_stage == "waiting_for_image_description":
            description = "" if user_msg.lower() in SKIP_WORDS else user_msg
            return self._file_image_complaint(
                {**state.entities, "complaint_description": description},
                state.entities.get("image_urls") or []
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
                            "complaint_items": [],
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
                    "image_urls": state.entities.get("image_urls") or [],
                    "items": state.entities.get("complaint_items") or []
                }
            )

        # 6. Waiting for Missing Item Details
        if current_stage == "waiting_for_missing_details":
            order_id = state.entities.get("order_id")
            dispatch_missing = state.entities.get("dispatch_missing_items") or []
            is_cod = state.entities.get("is_cod", False)
            refund_status = state.entities.get("refund_status")
            selected_items = state.entities.get("complaint_items") or []
            additional = user_msg.strip()
            no_additional = additional.lower() in ["no", "none", "nope", "n", "no.", "nahi", "nahi."]

            detail_parts = ["Sub-category: Missing Item."]
            if selected_items:
                missing = ", ".join(f"{item['name']} (Qty: {item.get('complaint_qty', 1):g})" for item in selected_items)
                detail_parts.append(f"Items reported missing by customer: {missing}.")
            if dispatch_missing:
                detail_parts.append(f"Items missing at dispatch: {', '.join(dispatch_missing)}.")
                if is_cod:
                    detail_parts.append("Payment: COD (not charged for dispatch-missing items).")
                else:
                    detail_parts.append(f"Refund status for dispatch-missing items: {refund_status or 'not yet initiated'}.")
            if not no_additional:
                label = "Details" if selected_items else "Additional items not received by customer"
                detail_parts.append(f"{label}: {additional}.")

            # When the customer picked the missing items, the ticket is always filed.
            if no_additional and not dispatch_missing and not selected_items:
                return FlowResponse(
                    status="completed",
                    response="Thank you. No missing items were reported. If you need further help, feel free to reach out.",
                    updated_state={"current_flow": None, "current_stage": None}
                )

            if no_additional and is_cod and dispatch_missing and not selected_items:
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
                    "image_urls": [],
                    "items": selected_items
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
                    "image_urls": [],
                    "items": state.entities.get("complaint_items") or []
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
                    "image_urls": [],
                    "items": state.entities.get("complaint_items") or []
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
                            response="I’m sorry, but more than 7 days have passed since your order was delivered. According to Naheed’s policy, we’re unable to register a complaint after this period. You can contact our Customer Support team at (021) 111-624-333 for further assistance.",
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
            "complaint_description": user_description,
            # Order items the customer picks for this complaint (if asked)
            "complaint_items": []
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

        if sub_category in ITEMS_NOT_REQUIRED_CATEGORIES:
            return self._ask_complaint_details(new_entities)
        return self._ask_for_items(new_entities, f"I have noted this as a {sub_category.lower()} complaint.")

    def _file_image_complaint(self, entities: dict, image_urls: list) -> FlowResponse:
        sub_cat = entities.get("complaint_sub_category", "Wrong Product")
        description = entities.get("complaint_description")
        details = f"Sub-category: {sub_cat}." + (f" Details: {description}" if description else "")
        return FlowResponse(
            status="completed",
            response="",
            updated_state={"current_flow": None, "current_stage": None},
            tool_request="create_complaint",
            tool_args={
                "order_id": entities.get("order_id"),
                "complaint_type": sub_cat,
                "details": details,
                "image_urls": image_urls,
                "items": entities.get("complaint_items") or []
            }
        )

    def _ask_for_items(self, new_entities: dict, prefix: str) -> FlowResponse:
        # Ask which of the order's items the complaint is about before the
        # description/photo. If the items can't be loaded, carry on without them.
        try:
            items = self.order_repository.get_order_items_for_complaint(new_entities.get("order_id", ""))
        except Exception:
            items = []
        if len(items) == 1 and items[0]["qty"] <= 1:
            # Only one item (qty 1) on the order, so it must be the one the
            # complaint is about - attach it without asking. With a larger qty
            # the customer is still asked how many units the complaint is for.
            return self._ask_complaint_details(
                {**new_entities, "complaint_items": [{**items[0], "complaint_qty": items[0]["qty"] or 1}]}
            )
        if items:
            return FlowResponse(
                status="waiting_for_input",
                response=_item_selection_prompt(items, prefix),
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_item_selection",
                    "entities": {**new_entities, "complaint_order_items": items}
                }
            )
        return self._ask_complaint_details(new_entities)

    def _ask_complaint_details(self, new_entities: dict) -> FlowResponse:
        # Asks for whatever the complaint type still needs (missing-item details,
        # a photo or a description) before the ticket is filed.
        order_id = new_entities.get("order_id", "")
        parent_order_id = new_entities.get("parent_order_id", "")
        category = new_entities.get("complaint_category")
        sub_category = new_entities.get("complaint_sub_category")
        items_selected = bool(new_entities.get("complaint_items"))

        if sub_category == OTHER_COMPLAINT_TYPE:
            prompt = "Please type your complaint in your own words and we will register it."
            return FlowResponse(
                status="waiting_for_input",
                response=f"Thank you. {prompt}" if items_selected else prompt,
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_other_complaint",
                    "entities": new_entities
                }
            )

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
                if items_selected:
                    lines.append("Please share any other details about your complaint. If there's nothing to add, type 'No'.")
                else:
                    lines.append("Were there any **other items** (besides the above) that you did not receive? If yes, please describe them. If no, type 'No'.")
            elif items_selected:
                lines.append("Thank you. Please describe your missing item issue in more detail. If there's nothing to add, type 'No'.")
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
            noted = "Thank you." if items_selected else f"I have noted this as a {sub_category.lower()} complaint."
            return FlowResponse(
                status="waiting_for_input",
                response=_image_upload_prompt(
                    f"{noted} Please upload one or more images related to your complaint."
                ),
                updated_state={
                    "current_flow": "complaint",
                    "current_stage": "waiting_for_image",
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
