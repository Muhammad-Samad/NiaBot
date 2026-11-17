"""detector.py - Decides when NiaBot hands a conversation to a human agent.

Works only on what the AI domains already report (`handoff_signals` on the
routed result, see router/domain_router.py, domains/policy/service.py and
domains/shopping/service.py) - the AI logic itself is not changed.

Reasons (stored in ncb_handoff_conversations.reason):
  customer_request   - the customer asked for a human (agent_handoff intent)
  unresolved         - NiaBot still couldn't help after its normal clarification
                       flow (operations escalation rules: repeated unknown intents,
                       repeated tool failures, frustrated customer)
  out_of_scope       - the policy bot couldn't answer (no grounded answer /
                       outside Naheed topics) - the last stop for anything the
                       permitted workflows don't cover
  technical_failure  - exception / API failure / search or policy RAG failure
  support_escalation - a flow's reply tells the customer to contact Naheed
                       customer support (cancel, complaint, refund, ...)
"""

import re

CUSTOMER_REQUEST = "customer_request"
UNRESOLVED = "unresolved"
OUT_OF_SCOPE = "out_of_scope"
TECHNICAL_FAILURE = "technical_failure"
SUPPORT_ESCALATION = "support_escalation"

# "please contact our Customer Support team at (021) 111-624-333", "contact
# Naheed customer support", "handled by our Customer Support team", the helpline.
_SUPPORT_RE = re.compile(
    r"(contact|reach out to|call)\s+(?:our\s+|naheed\s+|the\s+)?customer\s+support"
    r"|handled\s+by\s+our\s+customer\s+support"
    r"|111[\s-]?624[\s-]?333",
    re.IGNORECASE,
)
# A numbered choice that merely *offers* contacting support (cancel flow's
# "1. I will contact Customer Support / 2. I still want to cancel") - the
# customer hasn't chosen yet, so this is not an escalation.
_OPTION_PROMPT_RE = re.compile(r"please choose an option", re.IGNORECASE)

# Policy bot "couldn't answer" replies (config/policy.py fallback_message and
# the out-of-scope line in domains/policy/rag/prompts.py).
_POLICY_NO_ANSWER_RE = re.compile(
    r"couldn'?t find confirmed information|can only help with naheed",
    re.IGNORECASE,
)

# Splits a reply into sentences/lines, to drop the "contact support" ones.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?۔])\s+|\n+")


def detect(result: dict) -> tuple[str, str] | None:
    """(reason, detail) when this routed AI result should go to a human,
    else None. `result` is domain_router.route_message()'s return value."""
    signals = result.get("handoff_signals") or {}
    source = signals.get("source")

    if source == "operations":
        if signals.get("tool_request") == "agent_handoff":
            return CUSTOMER_REQUEST, "Customer asked for a customer support agent."
        if signals.get("tool_request") == "policy_rag":
            return _policy_reason(signals)
        if signals.get("escalation_offered"):
            return UNRESOLVED, "NiaBot could not resolve the request after repeated attempts."
        raw = signals.get("raw_response") or ""
        if _SUPPORT_RE.search(raw) and not _OPTION_PROMPT_RE.search(raw):
            return SUPPORT_ESCALATION, raw
        return None

    if source == "policy":
        return _policy_reason(signals)

    if source == "shopping" and signals.get("technical_failure"):
        return TECHNICAL_FAILURE, "Product search failed."

    return None


def _policy_reason(signals: dict) -> tuple[str, str] | None:
    if signals.get("policy_error"):
        return TECHNICAL_FAILURE, "Policy assistant failed to answer."
    raw = signals.get("raw_response") or ""
    if _POLICY_NO_ANSWER_RE.search(raw):
        return OUT_OF_SCOPE, "Policy assistant had no answer for this request."
    return None


def strip_support_contact(answer: str, reason: str) -> str:
    """The part of the AI reply still worth showing once a human takes over:
    sentences pointing the customer to the helpline are dropped (the
    customer is being connected right here instead). Replies that are only a
    'can't help' message are dropped entirely."""
    if reason in (CUSTOMER_REQUEST, OUT_OF_SCOPE, TECHNICAL_FAILURE) or not answer:
        return ""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(answer) if p and p.strip()]
    kept = [p for p in parts if not _SUPPORT_RE.search(p)]
    return " ".join(kept).strip()
