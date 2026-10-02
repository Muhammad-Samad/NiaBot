"""domain_router.py - Decides, per incoming chat message, whether the
shopping, operations or policy domain should handle it. This is what
makes the combined app a single conversation instead of separate bots bolted
together behind separate endpoints.

The numbered menu (see router/menu.py) is shown to the user client-side, by
templates/index.html's welcome screen, not sent as a chat turn — so every
real message, including the first one of a session, reaches this router and
is handled on its own merits instead of being swallowed by a canned greeting.

Routing rule, checked in order:
  1. A bare 1-2 digit number that matches a main-menu item is treated as a
     menu selection even if the bot never "opened" a menu in this session -
     the frontend's menu buttons send just the number. This only fires when
     no operations flow/menu is already in progress, so it can't misfire on
     a stray digit typed mid-flow.
     A pick from the Policy & FAQ sub-menu goes to the policy domain.
  2. Policy questions — domains/policy/intent.py's looks_like_policy()
     (return policy, delivery charges, loyalty points, payment methods,
     brands, store info...) go to the policy domain (ChromaDB RAG). This is
     checked BEFORE operations stickiness, so a policy question asked in the
     middle of an operations flow is answered without disturbing that flow:
     the operations state is left untouched and the answer ends with a
     reminder of where the flow left off, so the next reply continues it.
     Exception: a flow still waiting for an order ID / phone number is reset
     by any reply with no digits (other than "cancel"), and that reply is
     routed as a brand-new message.
  3. Stickiness — if this session is already in the middle of a stateful
     operations flow (order tracking waiting for an order id, a complaint
     collecting details, etc.) or an explicit awaiting_menu reply, keep
     routing to operations regardless of what the new message looks like.
     This reuses operations' own StateManager.current_flow, so the router
     doesn't need its own duplicate notion of "mid-flow".
  4. Otherwise, a lightweight keyword/pattern pre-classifier decides if the
     message LOOKS operational (order tracking/modify/cancel, refund,
     complaint, urgent delivery, talk-to-a-human, a bare order id, a
     greeting/goodbye). If so, hand off to operations, where its own
     LLM-based IntentParser does the real, nuanced classification. If that
     parser still says `general_policy`, operations hands the message to the
     policy domain itself (its `policy_rag` tool).
  5. Anything else — the default — goes to the shopping domain (product
     search). When shopping's off-topic guard rejects the message, the
     policy domain answers it instead: it covers store/policy questions the
     patterns above missed, and politely declines anything unrelated.
This keeps shopping as the primary/default bot, with operations and policy
handling the turns they clearly own.
"""

import re

from domains.operations import service as operations_service
from domains.policy import service as policy_service
from domains.policy.intent import looks_like_policy
from domains.shopping import service as shopping_service
from domains.shopping.query_engine import OFF_TOPIC_REPLY
from router import menu
from utils.logger import get_logger

logger = get_logger(__name__)

_ORDER_ID_RE = re.compile(r"\b\d{5,10}\b")
_HAS_DIGIT_RE = re.compile(r"\d")

_OPERATIONS_PATTERNS = [
    r"track.*order", r"where.*(is\s+)?(my\s+)?order", r"status.*order", r"order.*status",
    r"\b(my|mera|meri|mere)\s+(order|parcel)\b", r"\bwhen\b.*\border\b.*\bdeliver",
    r"modify.*order", r"change.*order", r"\bedit\b.*order", r"add.*item.*order", r"remove.*item.*order",
    r"cancel.*order", r"order.*cancel",
    r"\brefund\b", r"return.*(item|product|order)",
    r"\bcomplain(t)?\b", r"\bbroken\b", r"\bdamaged\b", r"wrong item", r"wrong product", r"missing item",
    r"jaldi.*(deliver|bhej)", r"urgent.*delivery", r"same.*day.*delivery", r"rush.*order", r"\bexpedite\b",
    r"\b(agent|representative|human|support staff)\b", r"talk to (a )?(person|someone)",
    r"^(hi|hello|hey|good morning|good afternoon|good evening)\b",
    r"^(bye|goodbye|see you)\b",
]


# "Who are you?" / "What's your name?" (English + Roman Urdu) - answered with
# NiaBot's self-introduction instead of being sent to product search.
_IDENTITY_PATTERNS = [
    r"\bwho\s+are\s+(you|u)\b", r"\bwhat('?s|\s+is)\s+(your|ur)\s+name\b",
    r"\b(your|ur)\s+name\b", r"\bare\s+you\s+(a\s+)?(bot|robot|human|real)\b",
    r"\b(tum|aap|ap)\s+(kaun|kon)\b", r"\b(tumhara|aapka|apka|tera)\s+naam\b",
    r"\bniabot\b", r"^\s*nia\s*[?!.]*\s*$",
]


def _asks_about_bot(message: str) -> bool:
    return any(re.search(p, message, re.IGNORECASE) for p in _IDENTITY_PATTERNS)


def _looks_operational(message: str) -> bool:
    msg = message.lower().strip()
    if _ORDER_ID_RE.search(msg):
        return True
    return any(re.search(p, msg, re.IGNORECASE) for p in _OPERATIONS_PATTERNS)


def _menu_result(answer: str, conversation_id: str | None) -> dict:
    return {
        "answer": answer,
        "products": [],
        "cart_action": None,
        "conversation_id": conversation_id,
        "domain": "menu",
    }


def _operations_result(sid: str, raw_message: str, conversation_id: str | None, forced_intent: str = None, forced_entities: dict = None) -> dict:
    result = operations_service.handle_message(sid, raw_message, forced_intent=forced_intent, forced_entities=forced_entities)
    # Operations hands a `general_policy` intent to the policy domain via its
    # policy_rag tool - record that turn as a policy one.
    handed_to_policy = (result.get("debug") or {}).get("tool_request") == "policy_rag"
    return {
        "answer": result["answer"],
        "products": [],
        "cart_action": None,
        # The Typesense conversation thread is untouched by an
        # operations turn — pass it straight through.
        "conversation_id": conversation_id,
        "domain": "policy" if handed_to_policy else "operations",
    }


# How the "back to where we left off" note names each operations flow.
_FLOW_LABELS = {
    "order_tracking": "tracking your order",
    "cancel_order": "cancelling your order",
    "complaint": "your complaint",
    "complaint_tracking": "checking your complaint status",
    "refund": "your refund request",
    "modify_order": "modifying your order",
    "special_request": "your delivery request",
}


def _resume_note(sid: str) -> str:
    flow, last_prompt = operations_service.get_resume_context(sid)
    label = _FLOW_LABELS.get(flow, "your earlier request")
    if last_prompt:
        note = f"Now, back to {label} - here's where we left off:\n\n{last_prompt}"
    else:
        note = f"Now, back to {label} - just reply to continue."
    return f"{note}\n\n(Type 'cancel' if you'd rather stop.)"


def _routed(result: dict, sid: str, reason: str) -> dict:
    logger.info(f"Routed to {result.get('domain')} (reason={reason}, sid={sid})")
    return result


def route_message(sid: str, message: str, conversation_id: str | None) -> dict:
    """Returns a normalized dict: {answer, products, cart_action,
    conversation_id, domain}. `domain` is only for logging/debugging."""
    stripped = (message or "").strip()
    normalized = stripped.lower()

    # "menu"/"help"/"options" always redisplays the main menu, wherever the
    # conversation currently stands.
    if normalized in menu.MENU_TRIGGER_WORDS:
        operations_service.set_menu_stage(sid, "main")
        return _routed(_menu_result(menu.format_main_menu(), conversation_id), sid, "menu_trigger")

    stage = operations_service.get_menu_stage(sid)
    choice = menu.parse_selection(stripped)

    # Implicit main-menu shortcut: no menu/flow currently pending, but the
    # message is a bare number matching a main-menu item - e.g. the user
    # tapped a number button on the frontend's welcome screen, or just typed
    # "1" cold. Numbers outside the menu's range fall through to normal
    # routing below instead of being treated as a menu pick.
    if stage is None and choice is not None and not operations_service.is_mid_flow(sid):
        kind, intent = menu.resolve_main_selection(choice)
        if kind == "submenu":
            operations_service.set_menu_stage(sid, "policy")
            return _routed(_menu_result(menu.format_policy_menu(), conversation_id), sid, "menu_shortcut")
        if kind == "flow":
            return _routed(_operations_result(sid, stripped, conversation_id, forced_intent=intent, forced_entities={}), sid, "menu_shortcut")

    if stage == "main" and choice is not None:
        kind, intent = menu.resolve_main_selection(choice)
        if kind == "submenu":
            operations_service.set_menu_stage(sid, "policy")
            return _routed(_menu_result(menu.format_policy_menu(), conversation_id), sid, "main_menu")
        if kind == "flow":
            operations_service.set_menu_stage(sid, None)
            return _routed(_operations_result(sid, stripped, conversation_id, forced_intent=intent, forced_entities={}), sid, "main_menu")
        # Not a valid main-menu number - reprompt rather than falling
        # through to shopping/operations, which wouldn't know what to do
        # with a bare "9".
        return _menu_result("That's not a valid option.\n\n" + menu.format_main_menu(), conversation_id)

    if stage == "policy" and choice is not None:
        question = menu.resolve_policy_selection(choice)
        operations_service.set_menu_stage(sid, None)
        if question:
            # A flow left waiting for an order ID / phone number is dropped
            # (the customer moved on to the menu). One further along is kept,
            # and the answer says so, or its next reply would look like it
            # came from nowhere.
            if operations_service.is_awaiting_identifier(sid):
                operations_service.reset_flow(sid)
            suffix = _resume_note(sid) if operations_service.is_mid_flow(sid) else None
            result = policy_service.handle_message(sid, stripped, conversation_id, entry="policy_menu", question=question, suffix=suffix)
            return _routed(result, sid, "policy_menu")
        return _menu_result("That's not a valid option.\n\n" + menu.format_policy_menu(), conversation_id)

    # Free text (not a menu digit) always escapes whatever menu was pending,
    # so a customer who ignores the menu still gets normal NLU handling.
    if stage:
        operations_service.set_menu_stage(sid, None)

    mid_flow = operations_service.is_mid_flow(sid)

    # The flow is waiting for an order ID / phone number and this reply has no
    # digits, so it can't be that answer - the customer moved on. Drop the
    # flow and route the message as a brand-new one (shopping, policy or
    # operations). "cancel"/"reset" still goes to operations, which resets
    # and confirms it.
    if (
        mid_flow
        and not _HAS_DIGIT_RE.search(message)
        and operations_service.is_awaiting_identifier(sid)
        and not operations_service.is_cancellation(message)
    ):
        flow, _ = operations_service.get_resume_context(sid)
        operations_service.reset_flow(sid)
        mid_flow = False
        logger.info(f"Reset operations flow '{flow}': reply had no order ID / phone number (sid={sid})")

    if not mid_flow and _asks_about_bot(message):
        return _routed(_operations_result(sid, message, conversation_id, forced_intent="general_query", forced_entities={}), sid, "identity")

    if looks_like_policy(message):
        if mid_flow:
            # Answer without touching the operations state, then point the
            # customer back to the flow - their next reply continues it.
            result = policy_service.handle_message(sid, message, conversation_id, entry="mid_flow", suffix=_resume_note(sid))
            return _routed(result, sid, "policy_question_mid_flow")
        return _routed(policy_service.handle_message(sid, message, conversation_id), sid, "policy_question")

    if mid_flow:
        return _routed(_operations_result(sid, message, conversation_id), sid, "mid_flow")
    if _looks_operational(message):
        return _routed(_operations_result(sid, message, conversation_id), sid, "operational_pattern")

    result = shopping_service.handle_message(sid, message, conversation_id)
    if result.get("answer") == OFF_TOPIC_REPLY:
        # Shopping found no shopping angle - let the policy domain answer
        # store/policy questions the patterns missed (it politely declines
        # anything unrelated to Naheed).
        result = policy_service.handle_message(sid, message, result.get("conversation_id"), entry="shopping_off_topic")
        return _routed(result, sid, "shopping_off_topic")
    result["domain"] = "shopping"
    return _routed(result, sid, "default")
