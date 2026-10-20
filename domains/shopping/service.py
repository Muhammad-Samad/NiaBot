"""service.py - Shopping domain message handling.

Ported from npk_shopping_chatbot/app.py's /api/chat route body. History
persistence was pulled out of here (see repository.save_conversation_message)
— the unified /api/chat route in app.py persists every turn once, regardless
of which domain (shopping or operations) produced it, so this function just
resolves the message and returns a normalized result dict.

Every message is understood IN CONTEXT (domains/shopping/conversation.py):
recent conversation + structured need + products on screen + long-term
customer memory -> one fresh, self-contained search, built from the full
updated need. If that LLM call fails, the older single-message pipeline
(query_engine.analyze_query + last_topic) answers instead, so a model outage
degrades follow-ups rather than breaking search.
"""

import re
import uuid

from config.memory import HISTORY_MESSAGES
from config.shopping import VECTOR_DISTANCE_THRESHOLD
from domains.shopping import conversation, query_engine, repository, search
from services import memory_service

NO_MATCH_REPLY = (
    "I couldn't find any products matching your request at Naheed.pk. "
    "Could you try describing what you're looking for differently?"
)

# Explicit low-price asks (English + Roman Urdu) — see _search_turn.
_BUDGET_WORDS = re.compile(
    r"\b(cheap(?:er|est)?|affordable|budget|economical|inexpensive|low[\s-]?(?:cost|price[sd]?)|"
    r"less expensive|too expensive|very expensive|sast[aie]|kam\s+qeemat|kam\s+price)\b",
    re.IGNORECASE,
)

PARTIAL_LIST_NOTE = (
    "(I couldn't find a close match for everything mentioned above at Naheed.pk right now, "
    "so I've shown only the items that matched well.)"
)

# "Already searched" items remembered for one need, so "show more" keeps
# moving on to new product types.
MAX_SEARCHED_ITEMS = 15


def _result(answer, products, cart_action, conversation_id):
    return {
        "answer": answer,
        "products": products,
        "cart_action": cart_action,
        "conversation_id": conversation_id,
    }


def handle_message(sid: str, user_msg: str, conversation_id: str | None = None, customer_key: str | None = None) -> dict:
    # client_conv_id is a conversation_id Typesense itself issued on a
    # previous turn.
    client_conv_id = (conversation_id or "").strip() or None
    # Fallback id purely for cart/session bookkeeping before any real
    # Typesense conversation_id exists yet — never sent to Typesense itself.
    tracking_conv_id = client_conv_id or str(uuid.uuid4())

    # Register/refresh the cart_session row up front so it exists for every
    # branch below, not just the "products found" path.
    repository.ensure_cart_session(sid, tracking_conv_id)

    ack_msg, ack_response = query_engine.is_acknowledgement(user_msg)
    if ack_msg:
        return _result(ack_response, [], None, tracking_conv_id)

    state = repository.get_conversation_state(sid)
    try:
        history = repository.get_recent_messages(sid, HISTORY_MESSAGES)
        shown = repository.get_last_shown_products(sid)
        memory = memory_service.get_context(customer_key)
        turn = conversation.understand(user_msg, state, history, shown, memory)
    except Exception as e:
        print(f"[shopping] contextual understanding failed, using single-message pipeline: {e}")
        return _handle_single_message(sid, user_msg, client_conv_id, tracking_conv_id)

    memory_service.remember_async(customer_key, turn.remember)

    if turn.action == "off_topic":
        return _result(query_engine.OFF_TOPIC_REPLY, [], None, tracking_conv_id)

    if turn.action in ("answer", "clarify"):
        # No search this turn, but the need may have moved on (e.g. a
        # clarifying question about a new request).
        if turn.need:
            searched = [] if turn.turn_type == "new" else state.get("searched", [])
            repository.set_conversation_state(sid, {**state, "need": turn.need, "searched": searched})
        products = [shown[n - 1] for n in turn.show_products if 1 <= n <= len(shown)]
        answer = turn.reply or "Could you tell me a bit more about what you're looking for?"
        return _result(answer, products, None, tracking_conv_id)

    return _search_turn(sid, user_msg, turn, state, tracking_conv_id)


def _budget_filter(need: dict) -> dict | None:
    budget = need.get("budget") if isinstance(need.get("budget"), dict) else {}

    def num(value):
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    lo, hi = num(budget.get("min")), num(budget.get("max"))
    return {"min": lo, "max": hi} if (lo is not None or hi is not None) else None


def _search_turn(sid, user_msg, turn, state, tracking_conv_id) -> dict:
    need = dict(turn.need or {})

    # An explicit number in THIS message ("under 2500") is parsed exactly by
    # regex and wins; otherwise the budget carried in the need applies, so a
    # budget set two turns ago still holds for "for women" / "show more".
    _, price_filter = query_engine.extract_price_filter(user_msg)
    explicit_price = price_filter is not None
    if not explicit_price:
        price_filter = _budget_filter(need)

    # "cheaper ones" carries no number — resolve it against what was
    # actually shown last turn. Only for ONE product type: across a list
    # (groceries, gifts) "cheaper than the cheapest card shown" would cap
    # every item at the price of e.g. a Rs 40 salt pack — the model picks
    # budget-friendly items/pack sizes for lists instead.
    direction = turn.price_direction if turn.kind == "product" else None
    if direction is None and turn.kind == "product" and turn.turn_type != "new":
        direction = query_engine.extract_relative_price_direction(user_msg)
    if direction and not explicit_price:
        price_range = repository.get_last_shown_price_range(sid)
        if price_range:
            lo, hi = price_range
            if direction == "cheaper":
                price_filter = {"min": None, "max": max(lo - 1, 0)}
            else:
                price_filter = {"min": hi + 1, "max": None}
    need["budget"] = price_filter or {"min": None, "max": None}

    # Budget request -> cheapest of the relevant matches first. The model
    # carries prefer_cheaper across refinements of the same need; the
    # keyword check backs it up when the model misses an explicit ask.
    need["prefer_cheaper"] = bool(need.get("prefer_cheaper")) or bool(_BUDGET_WORDS.search(user_msg))

    search_text = turn.search_text
    searched = state.get("searched", []) if turn.turn_type == "more" else []
    searched = (searched + (turn.items[:search.MAX_EXPANSION_ITEMS] or [turn.query]))[-MAX_SEARCHED_ITEMS:]
    # Saved before searching, so even a no-results turn keeps the need
    # ("try a higher budget" still knows what we're looking for).
    repository.set_conversation_state(sid, {"need": need, "searched": searched, "last_search": search_text})
    repository.set_last_topic(sid, search_text)

    try:
        # Never pass a conversation_id: search_text is already
        # self-contained, and a fresh search gets category matching.
        hits, conversation_result = search.typesense_search_products(
            search_text, conversation_id=None, price_filter=price_filter,
            cheapest_first=need["prefer_cheaper"],
        )
    except Exception as e:
        return _result(f"Sorry, something went wrong while searching: {e}", [], None, tracking_conv_id)

    new_conv_id = (conversation_result or {}).get("conversation_id") or tracking_conv_id
    if new_conv_id != tracking_conv_id:
        repository.ensure_cart_session(sid, new_conv_id)

    searched_count = len(hits)
    hits = _within_distance_threshold(hits)
    # The reply was written before the results existed and names every item
    # type searched — say so when some of them had no real match.
    partial_list = turn.kind == "list" and 0 < len(hits) < searched_count
    if hits and turn.turn_type == "more":
        # Prefer products the customer hasn't seen yet; if everything was
        # already shown, showing it again beats showing nothing.
        seen = repository.get_recently_shown_product_ids(sid)
        unseen = [h for h in hits if str(h.get("document", {}).get("id", "")) not in seen]
        hits = unseen or hits

    if not hits:
        return _result(turn.no_results_reply or NO_MATCH_REPLY, [], None, new_conv_id)

    answer = (
        turn.reply
        or (conversation_result or {}).get("answer")
        or query_engine.build_multi_item_answer(hits)
    )
    if partial_list:
        answer += f"\n\n{PARTIAL_LIST_NOTE}"
    return _result(answer, _hits_to_products(hits), None, new_conv_id)


def _within_distance_threshold(hits: list) -> list:
    """Drops weak semantic matches ("Wall Art" -> prayer mats). Typesense
    puts vector_distance at the top level of each hit (search.py reads it
    there too); a hit without one (no vector search) is kept."""
    if VECTOR_DISTANCE_THRESHOLD >= 1.0:
        return hits
    return [
        h for h in hits
        if h.get("vector_distance", 0) <= VECTOR_DISTANCE_THRESHOLD
    ]


def _hits_to_products(hits: list) -> list:
    products = []
    for hit in hits:
        doc = hit.get("document", {})

        raw_cat = doc.get("category", doc.get("categories", ""))
        if isinstance(raw_cat, list):
            raw_cat = ", ".join(str(c) for c in raw_cat)

        raw_img = doc.get("image", doc.get("image_url", doc.get("thumbnail", "")))
        if isinstance(raw_img, list):
            raw_img = raw_img[0] if raw_img else ""

        products.append({
            "product_id":   str(doc.get("id", doc.get("product_id", ""))),
            "sku":          str(doc.get("sku", "") or ""),
            "product_name": str(doc.get("name", doc.get("product_name", doc.get("title", "Unknown")))),
            "price":        repository.to_json_safe(doc.get("price", doc.get("final_price", 0))),
            "image_url":    str(raw_img or ""),
            "category":     str(raw_cat or ""),
            "url":          str(doc.get("url", doc.get("product_url", "")) or ""),
        })
    return products


# ─── Fallback: single-message pipeline (used when understand() fails) ─────────
def _handle_single_message(sid, user_msg, client_conv_id, tracking_conv_id) -> dict:
    # Price constraints ("under 2500") are pulled out with a regex before the
    # message ever reaches the LLM or Typesense.
    cleaned_msg, price_filter = query_engine.extract_price_filter(user_msg)

    if not cleaned_msg.strip():
        # The whole message was a price phrase ("under 500") — by definition
        # a follow-up fragment referencing whatever was already discussed.
        off_topic, expanded_search_query, is_followup = False, "", True
    else:
        off_topic, expanded_search_query, is_followup = query_engine.analyze_query(cleaned_msg)

    if off_topic:
        return _result(query_engine.OFF_TOPIC_REPLY, [], None, tracking_conv_id)

    # A comparative follow-up ("show me something cheaper") carries no
    # explicit number — resolve it from what was actually shown last turn.
    if price_filter is None and is_followup:
        direction = query_engine.extract_relative_price_direction(cleaned_msg)
        if direction:
            price_range = repository.get_last_shown_price_range(sid)
            if price_range:
                lo, hi = price_range
                if direction == "cheaper":
                    price_filter = {"min": None, "max": max(lo - 1, 0)}
                else:
                    price_filter = {"min": hi + 1, "max": None}

    # Only carry the prior Typesense conversation_id forward for a genuine
    # follow-up fragment.
    search_conv_id = client_conv_id if is_followup else None

    # Typesense's conversation feature only phrases its NL answer from
    # history — it does NOT rewrite "q". Fold in the last topic ourselves.
    if is_followup:
        last_topic = repository.get_last_topic(sid)
        if last_topic:
            expanded_search_query = f"{last_topic} {expanded_search_query}".strip()

    if not expanded_search_query.strip():
        return _result("What product or category are you looking for?", [], None, tracking_conv_id)

    try:
        hits, conversation_result = search.typesense_search_products(
            expanded_search_query, conversation_id=search_conv_id, price_filter=price_filter
        )
    except Exception as e:
        return _result(f"Sorry, something went wrong while searching: {e}", [], None, tracking_conv_id)

    if conversation_result is not None:
        # Single-item path: Typesense generated the reply itself.
        answer = conversation_result["answer"] or "Here's what I found for you."
        new_conv_id = conversation_result["conversation_id"] or tracking_conv_id
    else:
        # Multi-item path: deterministic reply, no LLM call.
        answer = query_engine.build_multi_item_answer(hits)
        new_conv_id = tracking_conv_id

    if new_conv_id != tracking_conv_id:
        repository.ensure_cart_session(sid, new_conv_id)

    hits = _within_distance_threshold(hits)
    if not hits:
        return _result(NO_MATCH_REPLY, [], None, new_conv_id)

    # Remember this turn's resolved (self-contained) search text as the topic
    # for whatever follow-up fragment comes next.
    repository.set_last_topic(sid, expanded_search_query)

    return _result(answer, _hits_to_products(hits), None, new_conv_id)
