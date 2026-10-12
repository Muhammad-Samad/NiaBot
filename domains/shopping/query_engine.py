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
_SYSTEM_PROMPT = """
You are a shopping-query understanding engine for a Pakistani retail store.
For every customer message return ONLY this JSON (no extra text):
{"off_topic": true|false, "query": "...", "is_followup": true|false}

## off_topic
- false: the message names/implies a product or category; is about browsing, buying, pricing, or the store; describes an occasion/activity that implies buying (birthday, gym, wedding, picnic, Eid, baby shower, anniversary...); or is a short follow-up in an ongoing shopping chat ("for men's", "make it cheaper", "only HP ones").
- true: ONLY when there is no shopping angle at all (chit-chat, weather, politics, coding, unrelated jokes).

## is_followup
Test: "Could a stranger with NO chat history tell what PRODUCT or CATEGORY to search for from this message alone?"
- No -> is_followup=true. Typical cases (illustrations, not an exhaustive list):
  - Brand only, with no product noun: "what about Nike?", "show me from canon", "do you have Samsung", "any Sony?" (a brand alone never defines a category).
  - Refinements without a product noun: budget ("under 5000", "cheaper ones"), audience ("for my daughter", "for men's"), attribute ("in white", "large size"), continuation ("show more", "yes"), reference ("the first one").
- Yes -> is_followup=false, even right after a different topic: "laptops under 150k", "canon printers", "mens perfumes", "something for my wife".

## query  (if off_topic=true, echo the message unchanged)
Pick the FIRST rule that applies:

A. Sunglasses -> "[Men's/Women's] [Brand] [Frame Color] [Lens Color] Lens Sunglasses"
   Include gender/brand/colors only if stated; default lens color Black/Brown when gender is given.
   "sunglasses" -> "Lens Sunglasses"; "men's sunglasses" -> "Men's Black Lens Sunglasses"; "brown sunglasses" -> "Brown Lens Sunglasses"

B. Broad/general request — any category, theme, activity, occasion, or list need ("gym products", "gym workout", "grocery", "breakfast", "stationery", "baby products", "birthday party", "camping", "water park") ->
   Return a comma-separated list of 6–10 SPECIFIC product names.
   DIVERSITY RULE (mandatory):
   - Cover several DIFFERENT sub-types of the theme (e.g. equipment, accessories, apparel, consumables, care items).
   - Max 2 items from the same sub-type. Never fill the list with variants of one kind (e.g. only supplements).
   - Order items so the most different sub-types come first.
   Examples:
   - "gym products" -> "Gym Belt, Gym Gloves, Dumbbells, Resistance Bands, Whey Protein, Pre Workout, Shaker Bottle, Yoga Mat, Gym Bag, Sports Towel"
   - "breakfast" -> "Paratha, Farm Eggs, Bread, Dairy Butter, Honey, Jam, Oatmeal, Tea, Juice"
   - "dairy products" -> "Milk, Yogurt, Cheese, Dairy Butter, Cream, Farm Eggs"
   - "birthday party" -> "Happy Birthday Decor, Balloons, Cake, Candles, Paper Plates, Paper Cups, Streamers, Chocolates, Gift Wrap"
   - "grocery" -> "Basmati Rice, Cooking Oil, Sugar, Salt, Daal Mash, Red Chili Powder, Tea, Milk, Noodles, Onions"
   - "Chicken Biryani Ingredients" -> "Chicken meat, Cooking Oil, Yogurt, Salt, Basmati Rice, Red Chili Powder, Biryani Masala, Tomatoes, Green Chilies, Onion (Pyaaz)"
   - "stationery" -> "Ball Pen, Pencil, Notebook, Eraser, Sharpener, Geometry Box, Highlighter, Glue Stick"
   Food names that also match non-food items MUST carry a food qualifier: "Dairy Butter" (never "Butter").

C. Gift for a person ("something for my wife", "gift for my dad") ->
   Same as B: 5–8 specific gift products suited to that recipient, across different sub-types. Never use the word "gift".
   - "something for my wife" -> "Chocolates, Perfume, Handbag, Jewelry Set, Makeup Kit, Skincare Set, Watch"
   - "gift for my husband" -> "Perfume, Wallet, Watch, Tie, Cufflinks, Grooming Kit"
   - "something for my dad" -> "Wallet, Watch, Perfume, Formal Shirt, Shaving Kit"

D. Specific product mention ("show cooking oils", "i need face wash", "whey protein") or a follow-up fragment ("for men's", "only large", "show more", "yes") ->
   Return the message UNCHANGED. Do not infer, rewrite, or complete it.

How to tell B from D: if the message names ONE concrete product type, use D. If it names a broad theme/area that contains many different product types (gym, kitchen, baby, party, school, travel, sports), use B.

## CRITICAL
"query" must be either the customer's exact words or real product names. NEVER output a category label, occasion name, or rule name (e.g. "Generic list request", "Gym Products", "Anniversary Gift", "Gift Request").
"""


def analyze_query(user_msg: str):
    """Returns (off_topic: bool, query: str, is_followup: bool). Fails open
    (off_topic=False, query=user_msg unchanged, is_followup=False) on any
    error, so a transient LLM hiccup blocks a legitimate shopping message
    rather than silently mishandling it — and defaulting is_followup to False
    means a failure never accidentally drags stale conversation context into
    an unrelated new search (see app.py, where is_followup gates whether the
    prior conversation_id is sent to Typesense)."""
    try:
        r = openai_client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=200,
        )
        result = json.loads(r.choices[0].message.content)
        off_topic = bool(result.get("off_topic", False))
        query = (result.get("query") or "").strip() or user_msg
        is_followup = bool(result.get("is_followup", False))
        print(f"[analyze_query] off_topic={off_topic} query={query!r} is_followup={is_followup}")
        return off_topic, query, is_followup
    except Exception as e:
        print(f"[analyze_query] Error: {e}")
        return False, user_msg, False


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
