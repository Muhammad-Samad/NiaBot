"""service.py - Shopping domain message handling.

Ported from npk_shopping_chatbot/app.py's /api/chat route body. History
persistence was pulled out of here (see repository.save_conversation_message)
— the unified /api/chat route in app.py persists every turn once, regardless
of which domain (shopping or operations) produced it, so this function just
resolves the message and returns a normalized result dict.
"""

import uuid

from config.shopping import VECTOR_DISTANCE_THRESHOLD
from domains.shopping import query_engine, repository, search


def _result(answer, products, cart_action, conversation_id):
    return {
        "answer": answer,
        "products": products,
        "cart_action": cart_action,
        "conversation_id": conversation_id,
    }


def handle_message(sid: str, user_msg: str, conversation_id: str | None = None) -> dict:
    # client_conv_id is a conversation_id Typesense itself issued on a
    # previous turn — it's what lets Typesense's native "Follow-up Questions"
    # feature continue that conversation's real history.
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

    # Price constraints ("under 2500") are pulled out with a regex before the
    # message ever reaches the LLM or Typesense.
    cleaned_msg, price_filter = query_engine.extract_price_filter(user_msg)

    if not cleaned_msg.strip():
        # The whole message was a price phrase ("under 500") — nothing left
        # for the LLM to classify, and by definition that's a follow-up
        # fragment referencing whatever product/category was already being
        # discussed.
        off_topic, expanded_search_query, is_followup = False, "", True
    else:
        off_topic, expanded_search_query, is_followup = query_engine.analyze_query(cleaned_msg)

    if off_topic:
        return _result(query_engine.OFF_TOPIC_REPLY, [], None, tracking_conv_id)

    # A comparative follow-up ("show me something cheaper") carries no
    # explicit number, so price_filter is still None here — resolve it from
    # what was actually shown last turn.
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

    # Only carry the prior Typesense conversation_id forward when this
    # message is genuinely a follow-up fragment that needs the prior turn's
    # context to resolve.
    search_conv_id = client_conv_id if is_followup else None

    # Typesense's conversation feature only uses history to phrase its NL
    # answer — it does NOT rewrite the "q" used for the actual embedding
    # search. Fold in the last resolved topic ourselves so the search text
    # stays self-contained.
    if is_followup:
        last_topic = repository.get_last_topic(sid)
        if last_topic:
            expanded_search_query = f"{last_topic} {expanded_search_query}".strip()

    if not expanded_search_query.strip():
        # A follow-up fragment with no prior topic to fall back on — nothing
        # meaningful to search for yet.
        clarify_answer = "What product or category are you looking for?"
        return _result(clarify_answer, [], None, tracking_conv_id)

    try:
        hits, conversation_result = search.typesense_search_products(
            expanded_search_query, conversation_id=search_conv_id, price_filter=price_filter
        )
    except Exception as e:
        return _result(f"Sorry, something went wrong while searching: {e}", [], None, tracking_conv_id)

    if conversation_result is not None:
        # Single-item path: Typesense generated the reply itself, grounded in
        # this conversation_id's real history.
        answer = conversation_result["answer"] or "Here's what I found for you."
        new_conv_id = conversation_result["conversation_id"] or tracking_conv_id
    else:
        # Multi-item path: deterministic reply, no LLM call.
        answer = query_engine.build_multi_item_answer(hits)
        new_conv_id = tracking_conv_id

    if new_conv_id != tracking_conv_id:
        repository.ensure_cart_session(sid, new_conv_id)

    filtered_hits = hits
    if VECTOR_DISTANCE_THRESHOLD < 1.0:
        filtered_hits = [
            h for h in filtered_hits
            if h.get("_rankingInfo", {}).get("vector_distance", 0) <= VECTOR_DISTANCE_THRESHOLD
        ]
    hits = filtered_hits

    if not hits:
        no_match_answer = (
            "I couldn't find any products matching your request at Naheed.pk. "
            "Could you try describing what you're looking for differently?"
        )
        return _result(no_match_answer, [], None, new_conv_id)

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

    # Remember this turn's resolved (self-contained) search text as the topic
    # for whatever follow-up fragment comes next.
    repository.set_last_topic(sid, expanded_search_query)

    return _result(answer, products, None, new_conv_id)
