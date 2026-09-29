"""domain_router.py - Decides, per incoming chat message, whether the
shopping domain or the operations domain should handle it. This is what
makes the combined app a single conversation instead of two bots bolted
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
  2. Stickiness — if this session is already in the middle of a stateful
     operations flow (order tracking waiting for an order id, a complaint
     collecting details, etc.) or an explicit awaiting_menu reply, keep
     routing to operations regardless of what the new message looks like.
     This reuses operations' own StateManager.current_flow, so the router
     doesn't need its own duplicate notion of "mid-flow".
  3. Otherwise, a lightweight keyword/pattern pre-classifier decides if the
     message LOOKS operational (order tracking/modify/cancel, refund,
     complaint, urgent delivery, talk-to-a-human, a bare order id, a
     greeting/goodbye). If so, hand off to operations, where its own
     LLM-based IntentParser does the real, nuanced classification.
  4. Anything else — the default — goes to the shopping domain (product
     search), which already has its own off-topic guard for pure chit-chat.
This keeps shopping as the primary/default bot, with operations handling the
turns it clearly owns, matching the ask of "integrate operational bot into
shopping bot" rather than two bots side by side.
"""

import re

from domains.operations import service as operations_service
from domains.shopping import service as shopping_service
from router import menu

_ORDER_ID_RE = re.compile(r"\b\d{5,10}\b")

_OPERATIONS_PATTERNS = [
    r"track.*order", r"where.*(is\s+)?(my\s+)?order", r"status.*order", r"order.*status",
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
    return {
        "answer": result["answer"],
        "products": [],
        "cart_action": None,
        # The Typesense conversation thread is untouched by an
        # operations turn — pass it straight through.
        "conversation_id": conversation_id,
        "domain": "operations",
    }


def route_message(sid: str, message: str, conversation_id: str | None) -> dict:
    """Returns a normalized dict: {answer, products, cart_action,
    conversation_id, domain}. `domain` is only for logging/debugging."""
    stripped = (message or "").strip()
    normalized = stripped.lower()

    # "menu"/"help"/"options" always redisplays the main menu, wherever the
    # conversation currently stands.
    if normalized in menu.MENU_TRIGGER_WORDS:
        operations_service.set_menu_stage(sid, "main")
        return _menu_result(menu.format_main_menu(), conversation_id)

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
            return _menu_result(menu.format_policy_menu(), conversation_id)
        if kind == "flow":
            return _operations_result(sid, stripped, conversation_id, forced_intent=intent, forced_entities={})

    if stage == "main" and choice is not None:
        kind, intent = menu.resolve_main_selection(choice)
        if kind == "submenu":
            operations_service.set_menu_stage(sid, "policy")
            return _menu_result(menu.format_policy_menu(), conversation_id)
        if kind == "flow":
            operations_service.set_menu_stage(sid, None)
            return _operations_result(sid, stripped, conversation_id, forced_intent=intent, forced_entities={})
        # Not a valid main-menu number - reprompt rather than falling
        # through to shopping/operations, which wouldn't know what to do
        # with a bare "9".
        return _menu_result("That's not a valid option.\n\n" + menu.format_main_menu(), conversation_id)

    if stage == "policy" and choice is not None:
        topic = menu.resolve_policy_selection(choice)
        operations_service.set_menu_stage(sid, None)
        if topic:
            return _operations_result(sid, stripped, conversation_id, forced_intent="general_policy", forced_entities={"policy_topic": topic})
        return _menu_result("That's not a valid option.\n\n" + menu.format_policy_menu(), conversation_id)

    # Free text (not a menu digit) always escapes whatever menu was pending,
    # so a customer who ignores the menu still gets normal NLU handling.
    if stage:
        operations_service.set_menu_stage(sid, None)

    if not operations_service.is_mid_flow(sid) and _asks_about_bot(message):
        return _operations_result(sid, message, conversation_id, forced_intent="general_query", forced_entities={})

    if operations_service.is_mid_flow(sid) or _looks_operational(message):
        return _operations_result(sid, message, conversation_id)

    result = shopping_service.handle_message(sid, message, conversation_id)
    result["domain"] = "shopping"
    return result
