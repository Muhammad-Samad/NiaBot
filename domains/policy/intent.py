"""intent.py - Recognises policy / FAQ / store-information questions so the
domain router can send them to the policy domain (see router/domain_router.py).

Pure regex, no LLM call, so it adds no latency to shopping or operations
turns. It is deliberately high-precision: a message is only treated as a
policy question when it names a policy/information topic, and it is vetoed
when it is really about the customer's OWN order or asks for an action the
operations domain performs (cancel, track, complain, return/refund request).
Policy questions this misses still reach the policy domain through two
safety nets: operations' own `general_policy` intent and shopping's
off-topic branch.

    looks_like_policy("what is your return policy")            -> True
    looks_like_policy("i want to return my order")             -> False (operations)
    looks_like_policy("what brands of cooking oil do you have") -> True
"""

import re

# Topics that are a policy/information question on their own.
_STRONG_PATTERNS = [
    r"\bpolic(y|ies)\b", r"\bfaqs?\b", r"\bterms\s*(and|&)\s*conditions\b", r"\bterms\s+of\s+(use|service)\b",
    r"\bprivacy\b", r"\bpersonal\s+data\b", r"\bdata\s+(protection|security)\b",
    # Returns / refunds / warranty
    r"\bnon[\s-]?returnable\b", r"\breturnable\b", r"\bwarrant(y|ies)\b", r"\bguarantee\b",
    r"\breturn\s+(period|window|time|timeline|days|process|procedure)\b",
    r"\brefund\s+(time|timeline|timelines|period|process|procedure|method|methods)\b",
    r"\bhow\s+long\b.*\brefund", r"\brefund\b.*\b(kitne|kitnay)\s+din\b",
    # Delivery / shipping / pickup
    r"\bexpress\s+(shipping|delivery)\b", r"\bstandard\s+(shipping|delivery)\b", r"\bsame[\s-]?day\s+(shipping|delivery)\b",
    r"\bfree\s+(delivery|shipping)\b", r"\bshipping\s+(charges?|fee|fees|cost|costs|time|methods?|options?)\b",
    r"\bdelivery\s+(charges?|fee|fees|cost|costs|time|times|timing|timings|days|areas?|cities)\b",
    r"\broad[\s-]?side\s+pick[\s-]?up\b", r"\bpick[\s-]?up\s+(method|option|point|service)\b", r"\bclick\s*(and|&)\s*collect\b",
    r"\bdo\s+(you|u)\s+deliver\b", r"\bdeliver(y)?\s+(to|in)\s+(lahore|islamabad|karachi|rawalpindi|other\s+cities|my\s+city|all\s+cities)\b",
    # Payment
    r"\bpayment\s+(methods?|options?|modes?)\b", r"\bcash\s+on\s+delivery\b", r"\bcod\b",
    r"\b(credit|debit)\s+card\b", r"\binstall?ments?\b", r"\bunion\s*pay\b", r"\bbank\s+transfer\b",
    r"\bpay\s+(by|with|via|through|online)\b",
    # Loyalty
    r"\bloyalty\b", r"\breward\s+points?\b", r"\bnaheed\s+(card|club)\b",
    r"\bpoints\b.*\b(earn|get|redeem|kitne)\b", r"\b(earn|get|redeem|kitne)\b.*\bpoints\b",
    # "what are the famous brands of cooking oil" - but not "do you have nike brand shoes" (shopping)
    r"\b(which|what|famous|popular|top|best|konse|kaun\s*se)\b.*\bbrands?\b",
    # Account / OTP
    r"\botp\b", r"\bverification\s+code\b", r"\b(forgot|reset|change)\s+(my\s+)?password\b",
    r"\b(create|register|delete|deactivate|close)\s+(an?\s+|my\s+)?account\b", r"\bsign\s*up\b",
    r"\b(can'?t|cannot|unable\s+to)\s+log\s*in\b", r"\blogin\s+(issue|problem|error)\b",
    # Company / stores / contact
    r"\babout\s+naheed\b", r"\bnaheed(\.pk)?\s+(kya|kia)\s+hai\b", r"\bwho\s+(owns|founded|started)\b",
    r"\b(naheed|your|store|stores)\s+(outlets?|branch(es)?)\b", r"\b(outlets?|branch(es)?)\s+(in|near|location|locations|address)\b",
    r"\bstore\s+(locations?|address|timings?|hours)\b",
    # Questions about the physical stores ("do you keep gold items in your physical store?")
    r"\b(physical|retail)\s+(stores?|shops?|outlets?)\b",
    r"\b(in|at)\s+(your|the|naheed'?s?)\s+(physical\s+)?(stores?|shops?|outlets?|branch(es)?|supermarket)\b",
    r"\b(opening|closing)\s+(time|times|hours)\b", r"\bhelpline\b",
    r"\b(contact|phone|whatsapp)\s+(number|details|info)\b", r"\bemail\s+address\b",
    r"\b(customer\s+(care|support|service)|support)\s+(number|email|hours|timings?)\b",
    r"\bhow\s+(can|do)\s+i\s+(contact|reach|call)\b",
    r"\bwhat\s+(do|does)\s+(you|u|naheed)\s+sell\b", r"\bproduct\s+categories\b",
]

# Topics that only count when the message is phrased as a question about how
# things work (see _INFO_FRAME_PATTERNS) - e.g. "return", "refund", "delivery".
_WEAK_PATTERNS = [
    r"\breturns?\b(?!\s+gifts?)", r"\brefunds?\b", r"\bexchange\b", r"\bdelivery\b", r"\bshipping\b",
    r"\bpick[\s-]?up\b", r"\bpayment\b", r"\bpoints\b", r"\baccount\b",
]

_INFO_FRAME_PATTERNS = [
    r"\?", r"^\s*(what|which|how|when|where|why|is|are|can|could|do|does|will|kya|kaise|kab|kahan|konse|kaun)\b",
    r"\b(procedure|process|rules?|allowed|eligible|possible|famous|popular|available)\b",
    r"\bcan\s+i\b", r"\bdo\s+(you|u)\s+have\b", r"\bis\s+there\b", r"\bhow\s+(to|do|does|can|much|many|long)\b",
]

# Messages about the customer's own order, or asking for an operations action.
_VETO_PATTERNS = [
    r"\b(my|mera|meri|mere|our)\s+(order|parcel|package|shipment|refund|complaint|ticket)\b",
    r"\btrack", r"\bcomplain", r"\bdamaged?\b", r"\bbroken\b", r"\bexpired?\b", r"\bleak",
    r"\bwrong\s+(item|product)\b", r"\bmissing\s+(item|product)\b",
    r"\b(i\s+want|i\s+need|i\s+would\s+like|i'?d\s+like|i\s+wanna|please|plz|kindly)\b.*\b(cancel|return|refund|exchange|replace)",
    r"\b(cancel|return|refund|exchange|replace)\b.*\b(karna|karni|karwana|karwani)\s+hai\b",
    r"\b(cancel|return|refund|exchange)\b.*\bchahiye\b",
]

# A real order id (5-10 digits) means the message is about one specific order.
_ORDER_ID_RE = re.compile(r"\b\d{5,10}\b")

# Asking about a policy explicitly ("what's your refund policy for my order?")
# overrides the vetoes - the customer wants the policy, not the action.
_EXPLICIT_POLICY_RE = re.compile(r"\b(polic(y|ies)|faqs?|terms\s*(and|&)\s*conditions)\b", re.IGNORECASE)

_STRONG = [re.compile(p, re.IGNORECASE) for p in _STRONG_PATTERNS]
_WEAK = [re.compile(p, re.IGNORECASE) for p in _WEAK_PATTERNS]
_INFO_FRAME = [re.compile(p, re.IGNORECASE) for p in _INFO_FRAME_PATTERNS]
_VETO = [re.compile(p, re.IGNORECASE) for p in _VETO_PATTERNS]


def looks_like_policy(message: str) -> bool:
    msg = (message or "").strip()
    if not msg:
        return False
    if _EXPLICIT_POLICY_RE.search(msg):
        return True
    if _ORDER_ID_RE.search(msg) or any(p.search(msg) for p in _VETO):
        return False
    if any(p.search(msg) for p in _STRONG):
        return True
    return any(p.search(msg) for p in _WEAK) and any(p.search(msg) for p in _INFO_FRAME)
