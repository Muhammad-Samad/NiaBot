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
  2. A flow still waiting for an order ID / phone number is reset by any
     reply with no digits (other than "cancel"), and that reply is routed as
     a brand-new message. "Who are you?" goes to operations' introduction.
  3. Stickiness — if this session is already in the middle of a stateful
     operations flow (order tracking waiting for an order id, a complaint
     collecting details, etc.), keep routing to operations regardless of
     what the new message looks like. Exception: a policy question
     (domains/policy/intent.py's looks_like_policy()) is answered by the
     policy domain without disturbing the flow, and the answer ends with a
     reminder of where the flow left off, so the next reply continues it.
  4. "cancel"/"reset" and a bare order ID always go to operations.
  5. Everything else is classified by ONE LLM call (_llm_route): operations'
     IntentParser, whose prompt also returns the domain (shopping /
     operations / policy), given which assistant replied last and any order
     verified this session. For operations the same result is passed on as
     `pre_parsed`, so the message isn't classified twice.
  6. Fallback when the LLM router is disabled (LLM_ROUTER_ENABLED=false),
     fails, or is below LLM_ROUTER_MIN_CONFIDENCE: keyword patterns -
     looks_like_policy() -> policy, _looks_operational() or a follow-up about
     a verified order -> operations, otherwise shopping.
Whenever shopping's off-topic guard rejects a message, the policy domain
answers it instead (it politely declines anything unrelated to Naheed).
"""

import os
import re
from collections import OrderedDict

from ai.prompts import ROUTING_CONTEXT_INJECTION
from domains.operations import service as operations_service
from domains.policy import service as policy_service
from domains.policy.intent import looks_like_policy
from domains.shopping import service as shopping_service
from domains.shopping.query_engine import OFF_TOPIC_REPLY
from router import menu
from utils.logger import get_logger

logger = get_logger(__name__)

_ORDER_ID_RE = re.compile(r"\b\d{10,12}\b")
_HAS_DIGIT_RE = re.compile(r"\d")
# A number next to price wording ("under 150000", "rs 5000", "50k budget") is
# a budget for product search, not an order ID.
_PRICE_CONTEXT_RE = re.compile(
    r"\b(under|below|above|over|less\s+than|more\s+than|upto|up\s+to|within|max(imum)?|min(imum)?|"
    r"between|around|budget|price|range|cheaper|rs\.?|pkr|rupees?|se\s+kam|se\s+zyada|tak)\b"
    r"|\d\s*k\b|\d\s*(rs|pkr)\b",
    re.IGNORECASE,
)

_OPERATIONS_PATTERNS = [
    r"track.*order",
    r"where.*(is\s+)?(my\s+)?order",
    r"status.*order",
    r"order.*status",
    r"\b(my|mera|meri|mere)\s+(order|parcel)\b",
    r"\b(mujhe|mujhy|mujy|mujhay|mjhe|mjhy|mujhko|humein|hamein|hume|hamara|humara|hamari|humari|apna|apni)\s+(order|parcel)\b",
    r"\b(order|parcel)\b.*\b(kahan|kahaan|kidhar|kidr|kab|kb|nahi\s+mila|nai\s+mila|nahi\s+aya|nai\s+aya|nahi\s+aaya|abhi\s+tak|status)\b",
    r"\b(kahan|kahaan|kidhar|kidr|kab|kb)\b.*\b(order|parcel)\b",
    r"\bwhen\b.*\border\b.*\bdeliver",
    r"modify.*order",
    r"change.*order",
    r"\bedit\b.*order",
    r"add.*item.*order",
    r"remove.*item.*order",
    r"cancel.*order",
    r"order.*cancel",
    r"\bcancel\s*(it|this|that|kar|kr|karo|kro|kardo|krdo|kardain|kardein|krdein|karna|krna|karni|krni)\b",
    r"\b(isy|isay|isey|ise|isse|isko|ye|yeh|usy|usay|usko)\s+(ko\s+)?cancel\b",
    r"\brefund\b",
    r"return.*(item|product|order)",
    r"\bcomplain(t)?\b",
    r"\bbroken\b",
    r"\bdamaged\b",
    r"wrong item",
    r"wrong product",
    r"missing item",
    r"jaldi.*(deliver|bhej)",
    r"urgent.*delivery",
    r"same.*day.*delivery",
    r"rush.*order",
    r"\bexpedite\b",
    r"\b(agent|representative|human|support staff)\b",
    r"talk to (a )?(person|someone)",
    r"^(hi|hello|hey|good morning|good afternoon|good evening)\b",
    r"^(bye|goodbye|see you)\b",
]


# "Who are you?" / "What's your name?" (English + Roman Urdu) - answered with
# NiaBot's self-introduction instead of being sent to product search.
_IDENTITY_PATTERNS = [
    r"\bwho\s+are\s+(you|u)\b",
    r"\bwhat('?s|\s+is)\s+(your|ur)\s+name\b",
    r"\b(your|ur)\s+name\b",
    r"\bare\s+you\s+(a\s+)?(bot|robot|human|real)\b",
    r"\b(tum|aap|ap)\s+(kaun|kon)\b",
    r"\b(tumhara|aapka|apka|tera)\s+naam\b",
    r"\bniabot\b",
    r"^\s*nia\s*[?!.]*\s*$",
]


# Follow-ups about an order the customer already verified this session, which
# don't say "order" ("iska status?", "ye kab aayega", "cancel kardo").
_VERIFIED_ORDER_FOLLOWUP_PATTERNS = [
    r"\bcancel",
    r"\b(status|track|tracking|deliver(y|ed)?|dispatch(ed)?|shipped|rider|courier|parcel)\b",
    r"\b(kab|kahan|kidhar|kidr)\b.*\b(aa?ye?ga|aa?ye?gi|milega|milegi|pohanch|pohnch|pahunch|hai)\b",
    r"\b(aa?ye?ga|aa?ye?gi|milega|milegi)\b",
    r"\b(refund|return|replace|exchange|modify|change|complain(t)?)\b",
    r"\b(iska|uska|iski|uski|iske|uske|isy|isay|usy|usay|isko|usko)\b",
]


def _is_verified_order_followup(message: str) -> bool:
    return any(re.search(p, message, re.IGNORECASE) for p in _VERIFIED_ORDER_FOLLOWUP_PATTERNS)


def _asks_about_bot(message: str) -> bool:
    return any(re.search(p, message, re.IGNORECASE) for p in _IDENTITY_PATTERNS)


def _looks_operational(message: str) -> bool:
    msg = message.lower().strip()
    if _ORDER_ID_RE.search(msg) and not _PRICE_CONTEXT_RE.search(msg):
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


def _operations_result(
    sid: str,
    raw_message: str,
    conversation_id: str | None,
    forced_intent: str = None,
    forced_entities: dict = None,
    pre_parsed=None,
) -> dict:
    result = operations_service.handle_message(
        sid, raw_message, forced_intent=forced_intent, forced_entities=forced_entities,
        pre_parsed=pre_parsed,
    )
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


# sid -> (domain, answer) of the last routed reply, so the LLM router knows
# what a short reply ("medium", "yes", "2") is answering. In-memory, like
# operations' StateManager; oldest sessions are dropped past the cap.
_last_turns: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
_LAST_TURNS_CAP = 5000


def _routed(result: dict, sid: str, reason: str) -> dict:
    logger.info(f"Routed to {result.get('domain')} (reason={reason}, sid={sid})")
    if result.get("domain") in ("shopping", "operations", "policy"):
        _last_turns[sid] = (result["domain"], result.get("answer") or "")
        _last_turns.move_to_end(sid)
        if len(_last_turns) > _LAST_TURNS_CAP:
            _last_turns.popitem(last=False)
    return result


def _shopping_result(
    sid: str, message: str, conversation_id: str | None, customer_key: str | None, reason: str
) -> dict:
    result = shopping_service.handle_message(
        sid, message, conversation_id, customer_key=customer_key
    )
    if result.get("answer") == OFF_TOPIC_REPLY:
        # Shopping found no shopping angle - let the policy domain answer
        # store/policy questions the routing missed (it politely declines
        # anything unrelated to Naheed).
        result = policy_service.handle_message(
            sid, message, result.get("conversation_id"), entry="shopping_off_topic"
        )
        return _routed(result, sid, "shopping_off_topic")
    result["domain"] = "shopping"
    return _routed(result, sid, reason)


_LLM_ROUTER_ENABLED = os.getenv("LLM_ROUTER_ENABLED", "true").strip().lower() == "true"
try:
    _LLM_ROUTER_MIN_CONFIDENCE = float(os.getenv("LLM_ROUTER_MIN_CONFIDENCE", "0.6"))
except ValueError:
    _LLM_ROUTER_MIN_CONFIDENCE = 0.6

# Operations intents, for a classification that came back without a domain.
_OPERATIONS_INTENTS = {
    "agent_handoff", "cancel_order", "order_tracking", "refund", "complaint_tracking",
    "complaint", "modify_order", "special_request", "greeting", "goodbye",
}


def _llm_route(sid: str, message: str):
    """Classifies the message with the operations IntentParser, whose prompt
    also picks the domain. Returns the IntentResult (with .domain set), or
    None when the router is disabled, the LLM fails, or it isn't confident -
    the caller then falls back to the keyword patterns."""
    if not _LLM_ROUTER_ENABLED:
        return None

    routing_context = ""
    last = _last_turns.get(sid)
    if last:
        last_domain, last_answer = last
        routing_context = ROUTING_CONTEXT_INJECTION.format(
            last_domain=last_domain, last_answer=last_answer[:300].replace('"', "'")
        )

    try:
        decision = operations_service.classify(sid, message, routing_context)
    except Exception as e:
        logger.warning(f"LLM router unavailable, using keyword routing: {e} (sid={sid})")
        return None

    if not decision.domain:
        if decision.intent == "general_policy":
            decision.domain = "policy"
        elif decision.intent in _OPERATIONS_INTENTS:
            decision.domain = "operations"
    if not decision.domain or decision.confidence < _LLM_ROUTER_MIN_CONFIDENCE:
        logger.info(
            f"LLM router unsure (domain={decision.domain}, intent={decision.intent}, "
            f"confidence={decision.confidence}); using keyword routing (sid={sid})"
        )
        return None

    logger.info(
        f"LLM router: domain={decision.domain} intent={decision.intent} "
        f"confidence={decision.confidence} (sid={sid})"
    )
    return decision


def route_message(
    sid: str, message: str, conversation_id: str | None, customer_key: str | None = None
) -> dict:
    """Returns a normalized dict: {answer, products, cart_action,
    conversation_id, domain}. `domain` is only for logging/debugging.
    `customer_key` (services/memory_service.py) gives the shopping domain
    the customer's long-term memory."""
    stripped = (message or "").strip()
    normalized = stripped.lower()

    # "menu"/"help"/"options" always redisplays the main menu, wherever the
    # conversation currently stands.
    if normalized in menu.MENU_TRIGGER_WORDS:
        operations_service.set_menu_stage(sid, "main")
        return _routed(
            _menu_result(menu.format_main_menu(), conversation_id), sid, "menu_trigger"
        )

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
            return _routed(
                _menu_result(menu.format_policy_menu(), conversation_id),
                sid,
                "menu_shortcut",
            )
        if kind == "flow":
            return _routed(
                _operations_result(
                    sid,
                    stripped,
                    conversation_id,
                    forced_intent=intent,
                    forced_entities={},
                ),
                sid,
                "menu_shortcut",
            )

    if stage == "main" and choice is not None:
        kind, intent = menu.resolve_main_selection(choice)
        if kind == "submenu":
            operations_service.set_menu_stage(sid, "policy")
            return _routed(
                _menu_result(menu.format_policy_menu(), conversation_id),
                sid,
                "main_menu",
            )
        if kind == "flow":
            operations_service.set_menu_stage(sid, None)
            return _routed(
                _operations_result(
                    sid,
                    stripped,
                    conversation_id,
                    forced_intent=intent,
                    forced_entities={},
                ),
                sid,
                "main_menu",
            )
        # Not a valid main-menu number - reprompt rather than falling
        # through to shopping/operations, which wouldn't know what to do
        # with a bare "9".
        return _menu_result(
            "That's not a valid option.\n\n" + menu.format_main_menu(), conversation_id
        )

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
            result = policy_service.handle_message(
                sid,
                stripped,
                conversation_id,
                entry="policy_menu",
                question=question,
                suffix=suffix,
            )
            return _routed(result, sid, "policy_menu")
        return _menu_result(
            "That's not a valid option.\n\n" + menu.format_policy_menu(),
            conversation_id,
        )

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
        logger.info(
            f"Reset operations flow '{flow}': reply had no order ID / phone number (sid={sid})"
        )

    if not mid_flow and _asks_about_bot(message):
        return _routed(
            _operations_result(
                sid,
                message,
                conversation_id,
                forced_intent="general_query",
                forced_entities={},
            ),
            sid,
            "identity",
        )

    if mid_flow:
        if looks_like_policy(message):
            # Answer without touching the operations state, then point the
            # customer back to the flow - their next reply continues it.
            result = policy_service.handle_message(
                sid,
                message,
                conversation_id,
                entry="mid_flow",
                suffix=_resume_note(sid),
            )
            return _routed(result, sid, "policy_question_mid_flow")
        return _routed(
            _operations_result(sid, message, conversation_id), sid, "mid_flow"
        )

    # "cancel"/"reset" and a bare order ID are always operations - no LLM.
    if operations_service.is_cancellation(message) or operations_service.is_bare_order_id(sid, message):
        return _routed(
            _operations_result(sid, message, conversation_id), sid, "operational_rule"
        )

    decision = _llm_route(sid, message)
    if decision:
        if decision.domain == "operations":
            return _routed(
                _operations_result(sid, message, conversation_id, pre_parsed=decision),
                sid,
                "llm",
            )
        if decision.domain == "policy":
            return _routed(
                policy_service.handle_message(sid, message, conversation_id, entry="llm_router"),
                sid,
                "llm",
            )
        return _shopping_result(sid, message, conversation_id, customer_key, "llm")

    # Fallback when the LLM router is off, unavailable or unsure: keyword
    # patterns, with shopping as the default.
    if looks_like_policy(message):
        return _routed(
            policy_service.handle_message(sid, message, conversation_id),
            sid,
            "policy_question",
        )
    if _looks_operational(message):
        return _routed(
            _operations_result(sid, message, conversation_id),
            sid,
            "operational_pattern",
        )
    if operations_service.has_verified_order(sid) and _is_verified_order_followup(message):
        return _routed(
            _operations_result(sid, message, conversation_id),
            sid,
            "verified_order_followup",
        )
    return _shopping_result(sid, message, conversation_id, customer_key, "default")
