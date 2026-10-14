"""
query_engine.py – Understands a raw customer message BEFORE any Typesense
call is made.

Pipeline for one customer message:
    1. is_acknowledgement()   – "thanks" / "ok" replies. No LLM.
    2. extract_price_filter() – regex price-range parsing ("under 2500").
                                 No LLM — exact numeric thresholds are handled
                                 with Typesense's native filter_by on the
                                 plain numeric price field, never via the
                                 semantic embedding (see search.py).
    3. analyze_query()        – ONE LLM call that both classifies off-topic
                                 messages and expands/normalizes on-topic
                                 ones. Replaces what used to be two separate
                                 LLM calls (is_off_topic + expand_query).

build_multi_item_answer() is the deterministic (no-LLM) reply text for the
multi-item "shopping list" search path — see its docstring for why an LLM
call there was unnecessary.

DIRECT_PRODUCT_HINTS / needs_expansion() from the old app.py were removed:
that hardcoded keyword list was only ever a crude, unmaintainable proxy for
"is this a direct product mention or does it need expanding" — exactly the
distinction analyze_query() now makes itself, from the actual message, as
part of the same LLM call that also does off-topic classification.
"""

import json
import random
import re

from config.base import BOT_NAME
from config.shopping import openai_client, LLM_MODEL

# ─── Acknowledgements ─────────────────────────────────────────────────────────
_ACK_PATTERNS = [
    r"^(ok|okay|okayy?)$",
    r"^thanks?$",
    r"^thank\s*you$",
    r"^thank\s*you\s*so\s*much$",
    r"^thanks\s*a\s*lot$",
    r"^many\s*thanks$",
    r"^ok\s*thanks$",
    r"^okay\s*thanks$",
    r"^ok\s*thank\s*you$",
    r"^okay\s*thank\s*you$",
    r"^got\s*it$",
    r"^great$",
    r"^perfect$",
    r"^cool$",
    r"^nice$",
    r"^awesome$",
    r"^well\s*done$",
]


def is_acknowledgement(user_msg: str):
    msg = re.sub(r"[^\w\s]", "", user_msg.lower().strip())
    if any(re.fullmatch(pattern, msg) for pattern in _ACK_PATTERNS):
        return True, "Happy to help! Feel free to ask if you need anything else."
    return False, None


# ─── Price semantics (regex, no LLM) ──────────────────────────────────────────
# One price number, with optional Pakistani-retail currency wording/suffix on
# either side: "rs 2500", "PKR 2,500", "2500/-", "50k", "1.5 lac".
_NUM = r"(?:rs\.?|pkr|rupees?)?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand|lac|lakh)?\s*(?:rs\.?|pkr|rupees?)?\s*/?-?"

_PRICE_PATTERNS = [
    ("between", re.compile(rf"\bbetween\s+{_NUM}\s+(?:and|to|-)\s+{_NUM}", re.IGNORECASE)),
    ("under",   re.compile(rf"\b(?:under|below|less than|up ?to|within|not more than|max(?:imum)?(?: of)?)\s+{_NUM}", re.IGNORECASE)),
    ("over",    re.compile(rf"\b(?:above|over|more than|min(?:imum)?(?: of)?|at least|starting from)\s+{_NUM}", re.IGNORECASE)),
    ("around",  re.compile(rf"\b(?:around|approx(?:imately)?|about|near)\s+{_NUM}", re.IGNORECASE)),
]


def _parse_num(digits: str, suffix: str | None) -> float:
    value = float(digits.replace(",", ""))
    suffix = (suffix or "").lower()
    if suffix in ("k", "thousand"):
        value *= 1_000
    elif suffix in ("lac", "lakh"):
        value *= 100_000
    return value


def extract_price_filter(user_msg: str):
    """Finds a price constraint phrase in the message (if any) and strips it
    out. Returns (cleaned_msg, price_filter) where price_filter is
    {"min": float|None, "max": float|None} or None when no price phrase was
    found.

    Runs BEFORE analyze_query() so the LLM never sees the price wording — it
    would otherwise get folded into the expanded search text instead of
    becoming a proper Typesense filter_by."""
    for kind, pattern in _PRICE_PATTERNS:
        m = pattern.search(user_msg)
        if not m:
            continue

        if kind == "between":
            lo = _parse_num(m.group(1), m.group(2))
            hi = _parse_num(m.group(3), m.group(4))
            price_filter = {"min": min(lo, hi), "max": max(lo, hi)}
        elif kind == "under":
            price_filter = {"min": None, "max": _parse_num(m.group(1), m.group(2))}
        elif kind == "over":
            price_filter = {"min": _parse_num(m.group(1), m.group(2)), "max": None}
        else:  # around -> +/-20% band
            val = _parse_num(m.group(1), m.group(2))
            price_filter = {"min": val * 0.8, "max": val * 1.2}

        cleaned = user_msg[:m.start()] + " " + user_msg[m.end():]
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.")
        print(f"[extract_price_filter] {price_filter} from {user_msg!r} -> {cleaned!r}")
        # NOTE: when the price phrase was the ENTIRE message (e.g. "under
        # 500"), cleaned is "". Do NOT fall back to the original user_msg
        # here — that would put the price wording right back in front of the
        # LLM/embedding search, which is exactly what this function exists to
        # prevent. An empty cleaned message is a valid, meaningful result:
        # it tells the caller "this message was pure price filter, nothing
        # else" (see app.py, where that short-circuits the analyze_query
        # call and falls through to the last-topic follow-up handling).
        return cleaned, price_filter

    return user_msg, None


# ─── Relative price follow-ups ("cheaper", "more expensive") ─────────────────
# extract_price_filter() above only catches an explicit NUMBER ("under
# 2500"). A follow-up like "show me something cheaper" carries no number at
# all, so without this it silently resolves to price_filter=None — no price
# constraint whatsoever — and the next search can come back MORE expensive
# than what was just shown. This only detects the direction; app.py resolves
# it into an actual bound using the previous turn's shown product prices,
# since "cheaper" has no meaning without something to be cheaper than.
_CHEAPER_PATTERN = re.compile(
    r"\b(cheaper|less expensive|lower[\s-]?price[sd]?|more affordable|"
    r"budget[\s-]?friendly|inexpensive(?:r)?|lower budget|cheap(?:er)?\s+ones?)\b",
    re.IGNORECASE,
)
_EXPENSIVE_PATTERN = re.compile(
    r"\b(more expensive|pricier|higher[\s-]?end|high[\s-]?end|premium|"
    r"costlier|expensive\s+ones?)\b",
    re.IGNORECASE,
)


def extract_relative_price_direction(user_msg: str):
    """Returns "cheaper", "expensive", or None. Only checked by the caller
    when extract_price_filter() found no absolute number AND the message is
    a follow-up (is_followup) — a brand-new, self-contained query saying
    "cheap laptops" has no prior shown prices to be relative to anyway."""
    if _CHEAPER_PATTERN.search(user_msg):
        return "cheaper"
    if _EXPENSIVE_PATTERN.search(user_msg):
        return "expensive"
    return None


# ─── Off-topic guard + query expansion (ONE merged LLM call) ─────────────────
# Replaces the old separate is_off_topic() and expand_query() calls. Both were
# doing a full LLM round-trip on every message; folding them into one call
# halves the per-message LLM cost/latency without losing either rule set.
# Shared by _SYSTEM_PROMPT and the dedicated _LIST_PROMPT fallback below.
_LIST_RULES = """
  LIST RULES (all mandatory):
  1. 6–10 items. Variety: cover several DIFFERENT product types (equipment, accessories, clothing, consumables, care items). Max 2 of the same type.
  2. NEVER repeat the theme word in the items. For "gym products" write "Weight Lifting Belt", not "Gym Belt"; "Duffel Bag", not "Gym Bag".
     (Repeating the theme word makes the search return only one kind of product.)
  3. Use plain, common product names that a store would actually stock. No brands unless the customer named one.
  4. Food names that also match non-food items MUST carry a food qualifier: "Dairy Butter" (never "Butter") or "Chicken meat" (never "Chicken").
  5. For gifts, never use the word "gift".

  Examples:
  - "suggest me some gym products" / "gym related products" / "gym" -> ["Whey Protein", "Weight Lifting Belt", "Workout Gloves", "Yoga Mat", "Resistance Bands", "Shaker Bottle", "Sports Towel", "Duffel Bag"]
  - "baby products"                 -> ["Diapers", "Baby Wipes", "Feeding Bottle", "Baby Lotion", "Baby Shampoo", "Baby Cereal", "Pacifier", "Baby Powder"]
  - "kitchen items"                 -> ["Non Stick Pan", "Pressure Cooker", "Knife Set", "Cutting Board", "Storage Containers", "Spatula", "Dinner Set"]
  - "camping"                       -> ["Tent", "Sleeping Bag", "Torch", "Water Bottle", "Portable Stove", "Insect Repellent", "Cooler Box"]
  - "breakfast"                     -> ["Paratha", "Farm Eggs", "Bread", "Dairy Butter", "Honey", "Jam", "Oatmeal", "Tea", "Juice"]
  - "Chicken Biryani Ingredients"   -> ["Chicken meat", "Basmati Rice", "Yogurt", "Onion (Pyaaz)", "Tomato (Tamatar)", "Ginger Powder", "Garlic (Lehsan)", "Green Chilli", "Coriander (Dhania) leaves"]
  - "birthday party"                -> ["Balloons", "Cake", "Candles", "Paper Plates", "Paper Cups", "Streamers", "Chocolates", "Party Hats"]
  - "something for my wife"         -> ["Perfume", "Handbag", "Jewelry Set", "Makeup Kit", "Skincare Set", "Watch"]
"""

_SYSTEM_PROMPT = """
You are a shopping-query understanding engine for a Pakistani retail store.
For every customer message return ONLY this JSON (no extra text):
{"off_topic": true|false, "is_followup": true|false, "kind": "product"|"list"|"followup", "query": "...", "items": ["...", ...]}

## off_topic
- false: anything with a shopping angle — a product, category, theme, activity, occasion, store question, or a short follow-up in an ongoing shopping chat.
- true: ONLY when there is no shopping angle at all (chit-chat, weather, politics, coding, jokes).

## is_followup
Test: "Could a stranger with NO chat history tell what PRODUCT or CATEGORY to search for from this message alone?"
- No  -> true  (brand only: "any Samsung?"; audience: "for men's", "for women", "for kids"; attribute: "in white", "large size";
               continuation: "show more", "yes", "no"; reference: "the first one"; budget: "under 5000", "cheaper ones").
- Yes -> false ("gym products", "laptops under 150k", "something for my wife").
A follow-up is NEVER off_topic, even a bare "yes" or "no".

## kind
First strip filler words ("suggest me", "show me", "i want", "i need", "can you", "some", "please", "any", "give me", "recommend") and judge only the core that remains.
- "followup": is_followup is true. Check this FIRST — a fragment that names no product is a followup, never a list:
              "for men's", "for women", "in white", "only large", "show more", "yes", "cheaper ones", "what about Nike?", "any Samsung?".
- "product":  the core is exactly ONE concrete product type: "cooking oil", "face wash", "whey protein", "rice", "shampoo", "dumbbells", "laptop", "gym bag".
- "list":     EVERYTHING ELSE (the DEFAULT). Any word followed by "products", "items", "related", "things", "stuff", "essentials", "accessories",
              "supplies", "needs", "gear", "equipment", "kit", and any theme, area, activity, occasion, person/gift, or category:
              "gym products", "gym related products", "gym", "workout", "baby products", "kitchen items", "school supplies", "camping",
              "birthday party", "breakfast", "grocery", "something for my wife".
When unsure, choose "list".

## query  (kind "product" / "followup", or off_topic=true)
Return the original message UNCHANGED. Do not infer, rewrite, or complete it.
Sunglasses exception: "sunglasses"/"men's sunglasses" etc. -> "[Men's/Women's] [Brand] [Frame Color] [Lens Color] Lens Sunglasses"
(include parts only if stated; default lens Black/Brown when gender is given).

## items  (kind "list" only; otherwise [])
SPECIFIC product names to search for, one per array entry.
""" + _LIST_RULES + """
## CRITICAL
NEVER output a category label or rule name ("Gym Products", "Generic list request", "Gift Request") in query or items.
"""

# Used only when the message is clearly broad but analyze_query's main call
# still didn't produce a list (see _BROAD_REQUEST_PATTERN).
_LIST_PROMPT = """
You expand a customer's broad shopping request for a Pakistani retail store into specific products to search for.
Return ONLY this JSON (no extra text): {"items": ["...", ...]}
""" + _LIST_RULES

# Words that make a message a broad, multi-product request no matter how the
# LLM classified it. A single-item search for "gym products" lets the
# category matcher lock onto one category (e.g. Gym Bags) and every result
# comes back as that one product type, so these messages are always forced
# into the multi-item list path.
_BROAD_REQUEST_PATTERN = re.compile(
    r"\b(products?|items?|related|things|stuff|essentials|accessories|supplies|"
    r"needs|gear|equipment|collection|range|ideas)\b",
    re.IGNORECASE,
)

MIN_LIST_ITEMS = 3

# Short refinement fragments that are always follow-ups, whatever the LLM
# says. Seen alone (with no chat history) the LLM tends to misread these —
# "for women" as a gift list, "in white" as a list of white products, "yes"
# as off-topic — which would drop the previous topic from the next search.
_COLORS = (r"black|white|red|blue|green|yellow|pink|purple|orange|brown|grey|gray|"
           r"silver|golden|gold|beige|maroon|navy|cream|multicolou?r")
_FOLLOWUP_PATTERNS = [re.compile(p) for p in (
    # audience: "for men's", "only for women", "kids ones"
    r"(?:only |just )?(?:for )?(?:the )?(?:men'?s?|women'?s?|ladies|gents|male|female|boys?|girls?|kids?|children|babies|him|her)(?: ones?| only)?",
    # colour: "in white", "black ones", "red colour"
    rf"(?:in |only |any )?(?:{_COLORS})(?: colou?r| ones?| only)?",
    # size: "only large", "xl size"
    r"(?:in |only |any )?(?:small|medium|large|x{0,3}l|x?s|m|big|bigger|smaller)(?: size| ones?| only)?",
    # continuation / confirmation
    r"(?:yes|yeah|yup|yep|sure|ok(?:ay)? show|no|nope|nah|more|show more|show me more|more options|other options|"
    r"something else|any other|anything else|next|another one|others?)",
    # reference to a shown item
    r"(?:the )?(?:first|second|third|fourth|last|1st|2nd|3rd|4th) one",
)]


def is_followup_fragment(user_msg: str) -> bool:
    msg = re.sub(r"[^\w\s']", " ", user_msg.lower())
    msg = re.sub(r"\s+", " ", msg).strip()
    return any(p.fullmatch(msg) for p in _FOLLOWUP_PATTERNS)


# Generic list items that pull in off-category products (e.g. "Cheese" also
# matches "Kurkure Cheese Puff Balls") are swapped for a more specific search
# term. Keys are lowercase and matched against the WHOLE item, so "Cheddar
# Cheese" or "Chicken meat" are left alone. Add new entries here.
ITEM_SYNONYMS = {
    "chicken": "Chicken meat",
    "egg": "Farm Eggs",
    "eggs": "Farm Eggs",
    "cheese": "Shredded Cheese",
}


def _apply_item_synonyms(items: list[str]) -> list[str]:
    mapped = []
    for item in items:
        item = ITEM_SYNONYMS.get(item.lower(), item)
        if item.lower() not in (m.lower() for m in mapped):
            mapped.append(item)
    return mapped


def _clean_items(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    items = [re.sub(r"\s+", " ", str(i)).strip() for i in raw if str(i).strip()]
    return _apply_item_synonyms(items)


def _expand_to_list(user_msg: str) -> list[str]:
    """Dedicated list-only LLM call. Returns [] on any error."""
    try:
        r = openai_client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _LIST_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=200,
        )
        items = _clean_items(json.loads(r.choices[0].message.content).get("items"))
        print(f"[_expand_to_list] {user_msg!r} -> {items}")
        return items
    except Exception as e:
        print(f"[_expand_to_list] Error: {e}")
        return []


def analyze_query(user_msg: str):
    """Returns (off_topic: bool, query: str, is_followup: bool). Fails open
    (off_topic=False, query=user_msg unchanged, is_followup=False) on any
    error, so a transient LLM hiccup blocks a legitimate shopping message
    rather than silently mishandling it — and defaulting is_followup to False
    means a failure never accidentally drags stale conversation context into
    an unrelated new search (see app.py, where is_followup gates whether the
    prior conversation_id is sent to Typesense).

    For list requests, query is the items joined with ", " — that comma
    list is what routes search.py into its multi-item path."""
    try:
        r = openai_client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=250,
        )
        result = json.loads(r.choices[0].message.content)
        off_topic = bool(result.get("off_topic", False))
        is_followup = bool(result.get("is_followup", False))
        kind = str(result.get("kind") or "").lower()
        items = _clean_items(result.get("items"))
        query = (result.get("query") or "").strip() or user_msg
    except Exception as e:
        print(f"[analyze_query] Error: {e}")
        off_topic, is_followup, kind, items, query = False, False, "", [], user_msg

    # A follow-up is resolved against the previous topic (see service.py),
    # so it must stay the customer's own fragment — never expanded into a
    # fresh list, and never rejected as off-topic.
    if is_followup_fragment(user_msg):
        off_topic, is_followup = False, True
    if kind == "followup":
        is_followup = True
    if is_followup:
        kind, items, query = "followup", [], user_msg

    # Deterministic safety net: a broad request must always become a list,
    # even when the LLM classified it as a single product.
    wants_list = kind == "list" or (
        not off_topic and not is_followup and _BROAD_REQUEST_PATTERN.search(user_msg)
    )
    if wants_list and not off_topic:
        if len(items) < MIN_LIST_ITEMS:
            items = _expand_to_list(user_msg)
        if len(items) >= MIN_LIST_ITEMS:
            query = ", ".join(items)

    print(f"[analyze_query] off_topic={off_topic} kind={kind!r} query={query!r} is_followup={is_followup}")
    return off_topic, query, is_followup


OFF_TOPIC_REPLY = (
    f"I'm {BOT_NAME}, and I'm here to help you find and shop for products at Naheed.pk — "
    "feel free to ask me about anything you'd like to buy!"
)


# ─── Multi-item (shopping-list) reply text — deterministic, no LLM ────────────
_FOUND_REPLIES = [
    "I found some great options for you — take a look below!",
    "Here are some options that match what you're looking for.",
    "Got a few good matches for you — check them out below!",
]

_NOT_FOUND_REPLY = (
    "I couldn't find any products matching your request. "
    "Could you try describing what you're looking for differently?"
)


def build_multi_item_answer(hits: list) -> str:
    """Reply text for the multi-item (shopping-list) search path — e.g.
    "breakfast options" expanding into several comma-separated items.

    This used to be a third LLM call (generate_llm_answer), but its own
    rules already forbid mentioning any product name, brand, price, or
    availability detail (the UI renders the product cards separately) —
    which leaves nothing for an LLM to actually decide between "found
    something" and "found nothing". Templating it is instant and free
    instead of paying for a round-trip that could only ever pick from a
    couple of fixed outcomes anyway."""
    if not hits:
        return _NOT_FOUND_REPLY
    return random.choice(_FOUND_REPLIES)
