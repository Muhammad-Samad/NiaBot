"""
conversation.py – Context-aware understanding of one shopping turn.

query_engine.analyze_query() only ever saw the CURRENT message, so a
follow-up could only be resolved by gluing it onto the previous search text
("Perfume, Watch, Wallet" + "he is 25 year old"). That works for small
add-ons ("for men's", "in white") and breaks for anything that changes the
need itself: an age, an area ("for walls"), a correction ("he is male not
female").

understand() instead gives ONE LLM call the whole picture — recent
conversation (all domains), the structured shopping need so far, the
products on screen, and long-term customer memory — and gets back:
  * how the message relates to the conversation (turn_type),
  * what to do (search / answer / clarify / off_topic),
  * the FULL updated need, from which a fresh, self-contained search is
    built every time (never text-gluing),
  * the customer's language — search terms always come back in English,
    since the catalog is English only,
  * the reply text for non-search turns (a search turn's reply is written
    once the results are known — see reply.py),
  * durable customer facts worth remembering for future visits.

service.py hands the resulting query / item list to
search.typesense_search_products().
"""

import json
import re
from dataclasses import dataclass, field

from ai.json_chat import chat_json
from config.base import BOT_NAME
from config.memory import CONVERSATION_MODEL
from config.shopping import openai_client
from domains.shopping.query_engine import _LIST_RULES, _clean_items
from domains.shopping.search import MAX_EXPANSION_ITEMS as MAX_SEARCHED_ITEMS

ACTIONS = ("search", "answer", "clarify", "off_topic")

# Price wording never belongs in search text — the budget is a price filter,
# and words like "Affordable Rice" only blur the embedding. Stripped even
# though the prompt forbids them, because models still slip them in.
_PRICE_WORDS = re.compile(
    r"\b(cheap(?:er|est)?|affordable|budget(?:[\s-]friendly)?|economical|inexpensive|low[\s-]?(?:cost|priced?)|"
    r"pricey|expensive|costly|under\s+\d[\d,.]*\s*(?:k|rs|pkr)?|pkr|rs\.?)(?=\W|$)",
    re.IGNORECASE,
)


def _strip_price_words(text: str) -> str:
    return " ".join(_PRICE_WORDS.sub(" ", text).split()).strip(" ,-")
TURN_TYPES = ("new", "refine", "correct", "more", "reference", "recall", "smalltalk")

# A gap this long between two messages (or since the last one) is marked in
# the history shown to the model, so it doesn't treat a weeks-old exchange
# as the immediate context of a brand-new request.
_GAP_MARK_MINUTES = 180

_SYSTEM_PROMPT = f"""
You are the conversation brain of {BOT_NAME}, the shopping assistant of Naheed.pk — a Pakistani online store
(groceries, household, personal care, beauty, fashion, electronics, home decor, kitchen, toys, baby, sports and more).

Each turn you get: customer memory from earlier visits (may be empty), the current shopping need, the products
on screen, the recent conversation, and the NEW customer message. Read the NEW message IN THE CONTEXT of the
conversation, like an attentive human shop assistant, and return ONLY this JSON object:

{{
  "language": "...",
  "turn_type": "new" | "refine" | "correct" | "more" | "reference" | "recall" | "smalltalk",
  "action": "search" | "answer" | "clarify" | "off_topic",
  "need": {{
    "summary": "...",
    "recipient": {{"relation": null, "gender": null, "age": null}},
    "attributes": {{"brand": null, "color": null, "size": null, "material": null, "style": null}},
    "budget": {{"min": null, "max": null}},
    "prefer_cheaper": false
  }},
  "price_direction": null,
  "kind": "product" | "list",
  "query": "...",
  "items": ["...", ...],
  "reply": "...",
  "show_products": [],
  "remember": []
}}

## Step 0 — language
The language AND script the customer writes in: "English", "Roman Urdu" (Urdu/Hindi in Latin letters, e.g.
"mujhe doodh chahiye"), "Urdu" (Urdu script), or another language's name ("Punjabi", "Sindhi", "Pashto", "Arabic").
Mixed Roman Urdu + English words ("han or kuch water gun dikhao") is "Roman Urdu". When the NEW message is
language-neutral ("ok", "yes", "5000", a brand name), use the language of the customer's recent messages.

## Step 1 — turn_type: how does the NEW message relate to the conversation?
- "new": a new need, unrelated to the current one ("now show me rice" after shoes). Start a fresh need — do NOT carry
  over the old recipient/attributes/budget (a stored profile fact such as a known size may still apply).
- "refine": adds or changes a detail of the current need — audience ("for men", "for my son"), age ("he is 25"),
  area/purpose ("for walls", "for the kitchen"), color, size, brand ("what about Nike?", "any Samsung?"),
  material, style, budget ("under 5000", "cheaper ones").
- "correct": fixes something assumed or misunderstood ("he is male not female", "I meant phone covers, not phones",
  "no, for adults").
- "more": more/other options for the same need ("show more", "anything else?", "other options", or "yes" after
  you offered more).
- "reference": about a product on screen ("the second one", "is the first one available in black?",
  "how much is the 3rd?").
- "recall": about an earlier visit or earlier in the conversation ("what did I look at last time?",
  "show me that watch again", "same shampoo as before").
- "smalltalk": greetings, thanks, feelings, or personal info with no product request.
Short replies ("yes", "ok", "sure", "the black one", "husband") answer YOUR last message — interpret them against it.
A message that is self-contained but continues the same need is still "refine".

## Step 2 — action
- "search": products should be shown — new / refine / correct / more turns; a reference asking for a variant
  ("first one in black" -> search that product in black); a recall asking to see items again.
- "answer": reply in words only — questions about on-screen products answerable from their listed
  name/price/category, questions about earlier conversations, smalltalk. Never invent product facts that are not
  listed (stock, specs, warranty, delivery) — say you're not sure and offer options or support.
- "clarify": ask ONE short question only when you cannot search well without it. Main case: the product depends on
  the recipient's gender and it is unknown — clothes, outfits, perfume, grooming, shoes, jewelry for "my spouse",
  "my partner", "my friend", "my child", "my cousin", "my sibling". If a sensible search is possible, search and
  ask in the reply instead.
- "off_topic": nothing to do with shopping, the customer, or this conversation (politics, coding, weather, sports
  scores, jokes, homework).

## Step 3 — need: ALWAYS return the FULL updated need (everything still valid), not just the change
- summary: short English phrase of everything the customer currently wants, e.g. "gift ideas for 25-year-old son",
  "wall decoration for bedroom", "outfits for husband", "white Nike running shoes for men".
- recipient: who it is for. son/husband/brother/father/boy/he -> "male"; daughter/wife/sister/mother/girl/she ->
  "female". spouse/partner/friend/child/kid/cousin/sibling -> null (unknown) unless stated. age as a number or null.
- attributes: only what the customer said, or a stored profile fact that clearly applies.
- budget: PKR numbers ("under 5k" -> max 5000, "1.5 lac" -> 150000, "between 2000 and 3000"). Keep it while
  refining the same need; drop it on a new need.
- prefer_cheaper: true when the customer wants low-priced options — "cheap", "affordable", "budget", "sasta",
  "kam qeemat", or complains that what you showed is too expensive. Products are then shown cheapest first. Keep it
  true while refining the same need; false on a new need, or when they ask for premium/high-end items.
- price_direction: "cheaper" / "expensive" when cheaper or pricier options of ONE product type are wanted WITHOUT a
  number, else null. ALWAYS null for a list need (groceries, gifts, ingredients...) — one price limit can't fit
  different product types; instead choose budget-friendly items, as described in Step 4.

## Step 4 — search (action "search" only)
Build the search from the FULL updated need — NEVER by appending the new words to the previous search.
SEARCH TERMS ARE ALWAYS ENGLISH. The catalog is English only, so whatever language the customer writes in, query
and items (and need.summary) MUST be the English product names an English store listing would use.
- Translate the MEANING, never the sound: "chashmay"/"chashmy"/"عینک" -> "Sunglasses" (for a child "Kids
  Sunglasses"); "chawal" -> "Basmati Rice"; "chai ki patti"/"چائے کی پتی" -> "Black Tea"; "cheeni" -> "White Sugar"; "doodh" -> "Milk"; "dahi" -> "Yogurt"; "anday" -> "Farm Eggs";
  "joote" -> "Shoes"; "kapray" -> clothes for the recipient ("Men's Kurta Shalwar", "Women's Lawn Suit");
  "khilone" -> specific toys; "sabun" -> "Soap"; "bartan" -> "Cookware"; "pani wali bandooq" -> "Water Gun".
- Drop filler words of every language: "dikhao", "chahiye", "la do", "kr do", "ka saman", "wagera", "kuch",
  "or", "bhi", "mujhe", "please", "zara".
- Turn vague words into concrete product types using the context: "color" for a 5-year-old means colouring
  supplies ("Color Pencils", "Crayons"), not the word "Color"; "masalay" for biryani -> "Biryani Masala".
- A local name may follow the English one in brackets, as in the list examples ("Onion (Pyaaz)").
- kind "product": the need is ONE concrete product type. query = a short self-contained search phrase with the
  relevant details: "Men's Nike Running Shoes White", "Samsung Smartphone", "Basmati Rice", "Wall Clock".
  items = []. A single product type stays ONE query even though it has variants ("running shoes" -> query
  "Running Shoes", never a list of shoe variants) — the only exception is "more", below.
- kind "list": the need is broad (gifts, a room or area, an occasion, a theme, a recipe, "outfits", "decorating
  material", "X products"). items = specific product names, MOST RELEVANT FIRST (only the first
  {MAX_SEARCHED_ITEMS} are searched). query = "".
  * Fit every item to the recipient: a 25-year-old son -> adult men's items; a 5-year-old son -> toys and kids'
    items; a mother -> women's items.
  * Put gender/age words INTO gendered items ("Men's Perfume", "Women's Handbag", "Kids' Water Bottle");
    non-gendered items stay plain ("Bluetooth Earbuds").
  * Fit every item to the area/purpose: "decorating material for my room" -> general decor; refined "for walls"
    -> ONLY wall items ("Wall Art", "Wall Clock", "Photo Frames", "Wall Shelves", "Wall Stickers", "Wall Hanging").
  * Include brand/color/size in items only when the customer asked for them.
  * The list examples below are for an unknown audience — when the recipient's gender/age is known, add it.
  * Cheaper / budget list ("only cheap items", "less expensive groceries"): keep the same product types but
    pick everyday staple variants and smaller pack sizes that still fit the need, written as normal product names
    ("Cooking Oil 3 Litre", "Sugar 1 kg", "Tea 190g") — never price words.
  * Quantities: a single pack size, not a range ("2 to 3 liters" -> "Cooking Oil 3 Litre").
- "more": the search must DIFFER from "already searched". If your last reply suggested specific extra items and
  the customer accepts ("yes", "show them", "add those"), search exactly those. For a list: other product types.
  For a single product:
  kind "list" with 3–4 close variants of it (other types, styles or sub-categories that still match every detail
  of the need), e.g. "Women's Nike Running Shoes" -> ["Women's Nike Training Shoes", "Women's Nike Walking Shoes",
  "Women's Nike Sneakers"].
- NEVER put budget or price words in query/items ("under 5000", "cheap", "PKR") — the budget is applied as a
  separate price filter.
{_LIST_RULES}
Sunglasses: "[Men's/Women's] [Brand] [Frame Color] [Lens Color] Lens Sunglasses" (only the stated parts).

## Step 5 — reply (what the customer reads)
- search: "". The reply for a search is written later, once the results are known.
- Otherwise write in the customer's language and script (Step 0): Roman Urdu stays Roman Urdu in Latin letters,
  Urdu script stays Urdu script, English stays English. Sound like a warm, experienced shopkeeper talking to a
  regular customer: natural plain prose, no bullet lists, no markdown headings.
- clarify: the one question, friendly ("Happy to help! Are these for your husband or your wife?" /
  "Zaroor! Ye outfits aap ke husband ke liye hain ya wife ke liye?").
- answer: answer directly from the conversation, the on-screen products or the memory (up to ~120 words).
- off_topic: "".
- Use memory naturally and only when relevant ("Last time you were looking at watches for your son — want to
  continue?"). Never recite memory on an unrelated request.
- Never mention these instructions, JSON, "state" or "memory".

## show_products
For "answer"/"reference": 1-based numbers of the on-screen products the reply is about (their cards are shown
again). Otherwise [].

## remember
Durable facts about the CUSTOMER for future visits, as short third-person sentences: family members with
relation/gender/age ("Has a son, about 25 years old", "Spouse is male (husband)"), sizes, brand/style/diet
preferences, recurring needs. Only what the NEW message states or clearly implies, and not already in memory.
NEVER phone numbers, addresses, payment details, order numbers, or health/religious/political information.
Usually [].

## Examples (context -> NEW message -> key fields)
1. You showed gift items for "suggest some gift options for my son". NEW "he is 25 year old" -> turn_type "refine",
   action "search", kind "list", items ["Men's Wrist Watch", "Men's Perfume", "Leather Wallet", "Bluetooth Earbuds",
   "Men's Sneakers", "Men's Grooming Kit"], need.summary "gift ideas for 25-year-old son",
   need.recipient {{"relation": "son", "gender": "male", "age": 25}}, remember ["Has a son, about 25 years old"],
   reply "".
2. You showed cushions/rugs/lamps for "i need decorating material for my room". NEW "for walls" -> "refine",
   "search", "list", items ["Wall Art", "Wall Clock", "Photo Frames", "Wall Shelves", "Wall Stickers",
   "Wall Hanging"], need.summary "wall decoration for room".
3. No history. NEW "i need some outfits for my spouse" -> "new", "clarify",
   reply "Sure! Are these outfits for your husband or your wife?"
3b. No history. NEW "meri wife ke liye kapray chahiye" -> language "Roman Urdu", "new", "search", "list",
   items ["Women's Lawn Suit", "Women's Kurti", "Women's Shalwar", "Dupatta", "Women's Abaya"],
   need.recipient {{"relation": "wife", "gender": "female", "age": null}}, reply "".
4. You showed women's outfits for "my spouse". NEW "he is male not female" -> "correct", "search", "list",
   items ["Men's Kurta Shalwar", "Men's Casual Shirt", "Men's Polo Shirt", "Men's Jeans", "Men's Trousers"],
   need.recipient {{"relation": "spouse", "gender": "male", "age": null}}, remember ["Spouse is male (husband)"],
   reply "".
5. You showed running shoes. NEW "what about Nike?" -> "refine", "search", "product", query "Nike Running Shoes".
6. You showed Nike running shoes. NEW "for women, under 8000" -> "refine", "search", "product",
   query "Women's Nike Running Shoes", need.budget {{"min": null, "max": 8000}}.
7. You showed a gift list. NEW "show more" -> "more", "search", "list", items = other gift types not already searched.
8. NEW "how much is the second one?" -> "reference", "answer", show_products [2], reply gives product 2's listed price.
9. Memory "2026-10-02: looked at gift ideas for 25-year-old son". New chat, NEW "what was I looking for last time?"
   -> "recall", "answer", reply "Last time you were looking at gift ideas for your son — want me to show some again?"
10. You showed shoes. NEW "now i need cooking oil" -> "new", "search", "product", query "Cooking Oil"
    (need reset: no shoe details).
11. NEW "who won the cricket match?" -> "off_topic".
12. NEW "beef biryani banany ka saman la kr do" -> language "Roman Urdu", "new", "search", "list",
    items ["Beef Meat", "Basmati Rice", "Yogurt", "Onion (Pyaaz)", "Tomato (Tamatar)", "Biryani Masala",
    "Ginger Garlic Paste", "Green Chilli"], need.summary "beef biryani ingredients".
13. You showed toys for a 5-year-old daughter. NEW "han or kuch water gun, color, chachmy wagera dikhao" ->
    language "Roman Urdu", "more", "search", "list", items ["Water Gun Toy", "Color Pencils", "Crayons",
    "Kids Sunglasses"].
14. NEW "بچوں کے لیے دودھ" -> language "Urdu", "new", "search", "product", query "Kids Milk".
"""


@dataclass
class Turn:
    turn_type: str
    action: str
    need: dict
    price_direction: str | None
    kind: str
    query: str
    items: list[str] = field(default_factory=list)
    reply: str = ""
    show_products: list[int] = field(default_factory=list)
    remember: list[str] = field(default_factory=list)
    language: str = "English"

    @property
    def search_text(self) -> str:
        """What search.typesense_search_products() expects: one phrase, or a
        comma-joined item list (2+ items routes it to the multi-item path)."""
        return ", ".join(self.items) if self.kind == "list" else self.query


def _fmt_price(value) -> str:
    try:
        return f"Rs {float(value):,.0f}"
    except (TypeError, ValueError):
        return ""


def _fmt_gap(minutes: int) -> str:
    if minutes >= 2 * 1440:
        return f"{minutes // 1440} days later"
    if minutes >= 1440:
        return "1 day later"
    return f"{minutes // 60} hours later"


def _format_memory(memory: dict | None) -> str:
    memory = memory or {}
    facts = memory.get("facts") or []
    past = memory.get("past") or []
    if not facts and not past:
        return "(none)"
    lines = []
    if facts:
        lines.append("Facts:")
        lines += [f"- {f}" for f in facts]
    if past:
        lines.append("Earlier conversations (newest first):")
        lines += [f"- {p['date']}: {p['summary']}" for p in past]
    return "\n".join(lines)


def _format_history(history: list[dict]) -> str:
    if not history:
        return "(no earlier messages — this is the start of the chat)"
    lines = []
    prev_age = None
    for m in history:
        age = m.get("age_minutes")
        if prev_age is not None and age is not None and prev_age - age >= _GAP_MARK_MINUTES:
            lines.append(f"(… {_fmt_gap(prev_age - age)} …)")
        prev_age = age
        speaker = "customer" if m["role"] == "user" else "you"
        text = " ".join(str(m["message"]).split())
        # Your own replies are kept longer: a descriptive list reply ends
        # with the extra items it offered, which a following "yes" accepts.
        limit = 300 if m["role"] == "user" else 900
        if len(text) > limit:
            text = text[:limit] + "…"
        line = f"{speaker}: {text}"
        if m.get("products"):
            line += f"  [showed: {'; '.join(m['products'][:5])}]"
        lines.append(line)
    if prev_age is not None and prev_age >= _GAP_MARK_MINUTES:
        lines.append(f"(… {_fmt_gap(prev_age)}, the customer is back …)")
    return "\n".join(lines)


def _format_products(products: list[dict]) -> str:
    if not products:
        return "(none)"
    lines = []
    for i, p in enumerate(products, start=1):
        details = ", ".join(x for x in (_fmt_price(p.get("price")), p.get("category") or "") if x)
        lines.append(f"{i}. {p.get('product_name', '')}" + (f" — {details}" if details else ""))
    return "\n".join(lines)


def _format_state(state: dict) -> str:
    need = state.get("need") or {}
    searched = state.get("searched") or []
    return (
        f"Need: {json.dumps(need, ensure_ascii=False) if need else '(none yet)'}\n"
        f"Already searched for this need: {'; '.join(searched) if searched else '(nothing)'}"
    )


def build_user_prompt(message: str, state: dict, history: list[dict], shown_products: list[dict], memory: dict | None) -> str:
    return (
        f"## Customer memory (earlier visits)\n{_format_memory(memory)}\n\n"
        f"## Current shopping need\n{_format_state(state)}\n\n"
        f"## Products on screen (from your last product reply)\n{_format_products(shown_products)}\n\n"
        f"## Recent conversation (oldest first)\n{_format_history(history)}\n\n"
        f"## NEW customer message\n{message}"
    )


def _parse(raw: dict) -> Turn:
    turn_type = str(raw.get("turn_type") or "new").lower()
    if turn_type not in TURN_TYPES:
        turn_type = "new"
    action = str(raw.get("action") or "").lower()
    kind = str(raw.get("kind") or "product").lower()
    query = _strip_price_words(str(raw.get("query") or ""))
    items = _clean_items([s for s in (_strip_price_words(str(i)) for i in (raw.get("items") or [])) if s])

    # Make kind/query/items consistent so search always gets usable text.
    if kind == "list" and len(items) < 2:
        kind, query = "product", (items[0] if items else query)
    if kind != "list":
        kind = "product"
        if not query and items:
            kind = "list" if len(items) >= 2 else "product"
            query = "" if kind == "list" else items[0]
        if kind == "product":
            items = []

    if action not in ACTIONS:
        action = "search" if (query or items) else "answer"
    reply = str(raw.get("reply") or "").strip()
    language = " ".join(str(raw.get("language") or "").split()) or "English"
    if action == "search" and not (query or items):
        # Nothing usable to search for — ask instead of searching blind.
        action = "clarify"
        reply = reply or "What product or category are you looking for?"

    # Relative pricing only makes sense for one product type — see
    # service._search_turn.
    direction = raw.get("price_direction")
    direction = direction if direction in ("cheaper", "expensive") and kind == "product" else None

    show = []
    for n in raw.get("show_products") or []:
        try:
            show.append(int(n))
        except (TypeError, ValueError):
            continue

    remember = [" ".join(str(f).split()) for f in (raw.get("remember") or []) if str(f).strip()]
    need = raw.get("need") if isinstance(raw.get("need"), dict) else {}

    return Turn(
        turn_type=turn_type, action=action, need=need, price_direction=direction,
        kind=kind, query=query, items=items, reply=reply,
        show_products=show, remember=remember[:5], language=language,
    )


def understand(message: str, state: dict, history: list[dict], shown_products: list[dict], memory: dict | None = None) -> Turn:
    """Raises on LLM/JSON failure — service.py falls back to the legacy
    single-message pipeline then."""
    prompt = build_user_prompt(message, state, history, shown_products, memory)
    raw = chat_json(openai_client, CONVERSATION_MODEL, _SYSTEM_PROMPT, prompt, max_tokens=1000, temperature=0.2)
    turn = _parse(raw)
    print(f"[conversation.understand] {message!r} -> lang={turn.language} type={turn.turn_type} action={turn.action} "
          f"kind={turn.kind} search={turn.search_text!r} remember={turn.remember}")
    return turn
