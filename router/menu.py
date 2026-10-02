"""menu.py - Numbered-menu definitions for the operations domain.

Order operations (tracking, cancellation, complaints, refunds, modify) and
Policy/FAQ topics are a small, finite set, so instead of relying on the LLM
intent classifier (with the rule-based router as its fallback) to figure out
which one a customer means, they can reply with a plain number and be routed
deterministically - zero ambiguity, zero LLM call. Free-text messages are
untouched and still go through the normal LLM/router pipeline; this is a
fast path, not a replacement.

The main menu itself is shown to the user by templates/index.html's welcome
screen (buttons that send the number), not as a chat message - format_main_menu()
here is only used for the "menu"/"help"/"options" text trigger and for
reprompting after an invalid number.

Two levels, both driven by domains/operations/service.py's per-session
`awaiting_menu` flag ("main" or "policy"), consumed by router/domain_router.py:
  - Main menu: order operations + an entry into the Policy/FAQ sub-menu.
  - Policy sub-menu: policy topics, each answered by the policy domain
    (domains/policy, ChromaDB RAG) by asking it that topic's question.
"""

import re
from typing import Optional, Tuple

# A bare 1-2 digit number, optionally followed by '.', ')' or ':' - e.g.
# "2", "2.", "2)". Anything else (including real order IDs, which are
# 5-10 digits) is treated as free text and falls through to normal routing.
_SELECTION_RE = re.compile(r"^\s*([0-9]{1,2})\s*[.\):]?\s*$")

MENU_TRIGGER_WORDS = {"menu", "help", "options"}

# (label, intent, visible) - intent is None for the entry that opens the
# Policy/FAQ sub-menu instead of a flow directly. Entries with visible=False
# are hidden from the menu and can't be picked by number, but their flows
# stay intact (still reachable via free text) so they can be re-enabled by
# flipping the flag. Numbers are assigned consecutively to visible entries
# only - keep templates/index.html's MAIN_MENU in the same order.
_MAIN_MENU_ENTRIES = [
    ("Track My Order", "order_tracking", True),
    ("Cancel an Order", "cancel_order", True),
    ("File a Complaint", "complaint", True),
    ("Track My Complaint / Ticket", "complaint_tracking", True),
    ("Refund / Return Request", "refund", False),
    ("Modify Order", "modify_order", False),
    ("Policy & FAQ (Delivery, Payment, Returns, Warranty & more)", None, True),
]

# (number, label, intent) for the visible entries.
MAIN_MENU = [
    (number, label, intent)
    for number, (label, intent, _visible) in enumerate(
        (e for e in _MAIN_MENU_ENTRIES if e[2]), start=1
    )
]

# Number of the entry that opens the Policy/FAQ sub-menu.
POLICY_SUBMENU_CHOICE = next(n for n, _label, intent in MAIN_MENU if intent is None)

# (number, label, question asked of the policy domain)
POLICY_MENU = [
    (1, "Delivery & Shipping", "What are Naheed.pk's delivery and shipping options, charges and delivery times?"),
    (2, "Payment Methods", "What payment methods does Naheed.pk accept?"),
    (3, "OTP Verification", "How does OTP verification work, and what should I do if I don't receive the OTP?"),
    (4, "Loyalty Program", "How does the Naheed loyalty program work?"),
    (5, "Return Policy", "What is Naheed.pk's return policy?"),
    (6, "Warranty Policy", "What is Naheed.pk's warranty policy?"),
    (7, "Company Information", "Tell me about Naheed and Naheed.pk."),
]


def format_main_menu() -> str:
    lines = ["Please choose an option (reply with a number):"]
    for number, label, _intent in MAIN_MENU:
        lines.append(f"{number}. {label}")
    lines.append("")
    lines.append("You can also just type your question anytime - I'll do my best to help either way.")
    return "\n".join(lines)


def format_policy_menu() -> str:
    lines = ["Please choose a topic (reply with a number):"]
    for number, label, _question in POLICY_MENU:
        lines.append(f"{number}. {label}")
    lines.append("")
    lines.append("Type 'menu' to go back to the main menu.")
    return "\n".join(lines)


def parse_selection(message: str) -> Optional[int]:
    """Returns the chosen number if `message` is a bare number, else None."""
    if not message:
        return None
    match = _SELECTION_RE.match(message)
    return int(match.group(1)) if match else None


def resolve_main_selection(choice: int) -> Tuple[Optional[str], Optional[str]]:
    """Returns (kind, intent). kind is 'flow' (intent is the operations
    intent to run), 'submenu' (open the Policy/FAQ sub-menu, intent is None),
    or None if `choice` isn't a valid main-menu number."""
    for number, _label, intent in MAIN_MENU:
        if number == choice:
            return ("submenu", None) if intent is None else ("flow", intent)
    return None, None


def resolve_policy_selection(choice: int) -> Optional[str]:
    """Returns the policy-domain question for `choice`, else None."""
    for number, _label, question in POLICY_MENU:
        if number == choice:
            return question
    return None
