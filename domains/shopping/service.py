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

A search turn's reply is written AFTER the search (reply.compose), from the
products actually being displayed, in the customer's own language.
"""

import re
import uuid

from config.memory import HISTORY_MESSAGES
from config.shopping import VECTOR_DISTANCE_THRESHOLD
from domains.shopping import conversation, facets, query_engine, reply, repository, search
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


def handle_message(
    sid: str,
    user_msg: str,
    conversation_id: str | None = None,
    customer_key: str | None = None,
) -> dict:
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
        print(
            f"[shopping] contextual understanding failed, using single-message pipeline: {e}"
        )
        return _handle_single_message(sid, user_msg, client_conv_id, tracking_conv_id)

    memory_service.remember_async(customer_key, turn.remember)

    if turn.action == "off_topic":
        return _result(query_engine.OFF_TOPIC_REPLY, [], None, tracking_conv_id)

    if turn.action in ("answer", "clarify"):
        # No search this turn, but the need may have moved on (e.g. a
        # clarifying question about a new request).
        if turn.need:
            searched = [] if turn.turn_type == "new" else state.get("searched", [])
            repository.set_conversation_state(
                sid, {**state, "need": turn.need, "searched": searched}
            )
        products = [shown[n - 1] for n in turn.show_products if 1 <= n <= len(shown)]
        answer = (
            turn.reply or "Could you tell me a bit more about what you're looking for?"
        )
        return _result(answer, products, None, tracking_conv_id)

    return _search_turn(sid, user_msg, turn, state, history, tracking_conv_id)


def _budget_filter(need: dict) -> dict | None:
    budget = need.get("budget") if isinstance(need.get("budget"), dict) else {}

    def num(value):
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    lo, hi = num(budget.get("min")), num(budget.get("max"))
    return {"min": lo, "max": hi} if (lo is not None or hi is not None) else None


def _facet_filter(need: dict, kind: str) -> dict:
    """Exact catalog manufacturer/category values for the brand and category
    in the need. The category describes ONE product type, so it is only used
    for a single-product search — across a list (gifts, groceries) it would
    filter out every other item's products."""
    attributes = need.get("attributes") if isinstance(need.get("attributes"), dict) else {}
    brand = str(attributes.get("brand") or "").strip() or None
    category = str(need.get("category") or "").strip() or None
    return facets.resolve(brand=brand, category=category if kind == "product" else None)


def _search_turn(sid, user_msg, turn, state, history, tracking_conv_id) -> dict:
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
    need["prefer_cheaper"] = bool(need.get("prefer_cheaper")) or bool(
        _BUDGET_WORDS.search(user_msg)
    )

    search_text = turn.search_text
    searched_now = turn.items[: search.MAX_EXPANSION_ITEMS] or [turn.query]
    searched = state.get("searched", []) if turn.turn_type == "more" else []
    searched = (searched + searched_now)[-MAX_SEARCHED_ITEMS:]
    # Saved before searching, so even a no-results turn keeps the need
    # ("try a higher budget" still knows what we're looking for).
    repository.set_conversation_state(
        sid, {"need": need, "searched": searched, "last_search": search_text}
    )
    repository.set_last_topic(sid, search_text)

    try:
        # Never pass a conversation_id: search_text is already
        # self-contained.
        hits, conversation_result = search.typesense_search_products(
            search_text,
            conversation_id=None,
            price_filter=price_filter,
            cheapest_first=need["prefer_cheaper"],
            facet_filter=_facet_filter(need, turn.kind),
        )
    except Exception as e:
        return _result(
            f"Sorry, something went wrong while searching: {e}",
            [],
            None,
            tracking_conv_id,
        )

    new_conv_id = (conversation_result or {}).get("conversation_id") or tracking_conv_id
    if new_conv_id != tracking_conv_id:
        repository.ensure_cart_session(sid, new_conv_id)

    print(f"=======> before applying distance_threshold {len(hits)}")
    hits = _within_distance_threshold(hits)
    if hits and turn.turn_type == "more":
        # Prefer products the customer hasn't seen yet; if everything was
        # already shown, showing it again beats showing nothing.
        seen = repository.get_recently_shown_product_ids(sid)
        unseen = [
            h for h in hits if str(h.get("document", {}).get("id", "")) not in seen
        ]
        hits = unseen or hits

    print(
        f"=======> after applying distance_threshold and generating reply {len(hits)}"
    )
    answer, products = _reply_for_results(
        user_msg,
        turn.language,
        need,
        turn.turn_type,
        searched_now,
        hits,
        history,
    )
    print(f"=======> after generating reply {len(products)}")
    return _result(answer, products, None, new_conv_id)


def _brand_not_found(need: dict, hits: list) -> str | None:
    """The brand the customer asked for, when it was searched but none of the
    shown products could be filtered to it — so the reply says so instead
    of passing other brands off as it. Unknown (None) when the brand filter
    is switched off."""
    attributes = need.get("attributes") if isinstance(need.get("attributes"), dict) else {}
    brand = str(attributes.get("brand") or "").strip()
    if not brand or not hits or facets.MANUFACTURER_FIELD not in facets.ENABLED_FIELDS:
        return None
    if any(facets.MANUFACTURER_FIELD in (h.get(search.FILTERED_KEY) or []) for h in hits):
        return None
    return brand


def _reply_for_results(
    user_msg, language, need, turn_type, searched, hits, history
) -> tuple[str, list]:
    """The reply, written once we know exactly what will be displayed, plus
    the products to display (minus any the reply step judged to be the wrong
    product type), each with a short display name. Falls back to a template
    reply if that LLM call fails."""
    products = _hits_to_products(hits)
    matched = [list(h.get(search.MATCHED_ITEM) or []) for h in hits]
    try:
        composed = reply.compose(
            user_msg, language, need, turn_type, searched, products, matched, history,
            brand_not_found=_brand_not_found(need, hits),
        )
    except Exception as e:
        print(f"[shopping] reply composition failed, using template reply: {e}")
        return (
            query_engine.build_multi_item_answer(hits) if hits else NO_MATCH_REPLY
        ), products

    for i, name in composed.short_names.items():
        products[i]["display_name"] = name
    products = [p for i, p in enumerate(products) if i not in composed.drop]
    answer = composed.reply or (
        query_engine.build_multi_item_answer(products) if products else NO_MATCH_REPLY
    )
    return answer, products


def _within_distance_threshold(hits: list) -> list:
    """Drops weak semantic matches ("Wall Art" -> prayer mats). Typesense
    puts vector_distance at the top level of each hit (search.py reads it
    there too); a hit without one (no vector search) is kept."""
    if VECTOR_DISTANCE_THRESHOLD >= 1.0:
        return hits
    return [h for h in hits if h.get("vector_distance", 0) <= VECTOR_DISTANCE_THRESHOLD]


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

        name = str(doc.get("name", doc.get("product_name", doc.get("title", "Unknown"))))
        products.append(
            {
                "product_id": str(doc.get("id", doc.get("product_id", ""))),
                "sku": str(doc.get("sku", "") or ""),
                # Full catalog name: what the cart and orders record.
                "product_name": name,
                # What the card shows — replaced by the reply step's short
                # name when it gives one (see reply.compose).
                "display_name": reply.shorten_name(name),
                "price": repository.to_json_safe(
                    doc.get("price", doc.get("final_price", 0))
                ),
                "image_url": str(raw_img or ""),
                "category": str(raw_cat or ""),
                "url": str(doc.get("url", doc.get("product_url", "")) or ""),
            }
        )
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
        off_topic, expanded_search_query, is_followup = query_engine.analyze_query(
            cleaned_msg
        )

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
        return _result(
            "What product or category are you looking for?", [], None, tracking_conv_id
        )

    try:
        hits, conversation_result = search.typesense_search_products(
            expanded_search_query,
            conversation_id=search_conv_id,
            price_filter=price_filter,
        )
    except Exception as e:
        return _result(
            f"Sorry, something went wrong while searching: {e}",
            [],
            None,
            tracking_conv_id,
        )

    new_conv_id = (conversation_result or {}).get("conversation_id") or tracking_conv_id
    if new_conv_id != tracking_conv_id:
        repository.ensure_cart_session(sid, new_conv_id)

    hits = _within_distance_threshold(hits)
    if hits:
        # Remember this turn's resolved (self-contained) search text as the
        # topic for whatever follow-up fragment comes next.
        repository.set_last_topic(sid, expanded_search_query)

    searched = [i.strip() for i in expanded_search_query.split(",") if i.strip()]
    need = {"summary": expanded_search_query, "budget": price_filter or {}}
    # language=None: the reply step reads it off the customer's message.
    answer, products = _reply_for_results(
        user_msg, None, need, None, searched, hits, []
    )
    return _result(answer, products, None, new_conv_id)
