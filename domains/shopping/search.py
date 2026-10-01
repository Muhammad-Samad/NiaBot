"""
search.py – Typesense product retrieval.

Two paths, chosen by how many comma-separated items the (already expanded)
query resolves to:

  * Single item  – uses Typesense's native "Follow-up Questions" feature
    (conversation=true + conversation_model_id, plus conversation_id once one
    exists) so Typesense itself — using the real conversation_store history —
    rewrites a follow-up into a standalone search query and generates the
    reply text. We never build or forward a manual chat-history array
    ourselves.

  * Multi item (e.g. "breakfast options" -> Paratha, Egg, Bread, ...) – fans
    out into one plain, conversation-free search per item in a single
    multi_search call, since that doesn't map onto Typesense's single-query
    follow-up mechanism. The caller generates its own reply text for this
    path (see query_engine.build_multi_item_answer).

Price constraints ("under 2500") are applied as a Typesense filter_by on the
plain numeric PRICE_FIELD, never through the embedding — see
price_filter_to_filter_by().
"""

from urllib.parse import urlencode

import requests

from config.shopping import (
    TYPESENSE_HOST, TYPESENSE_PORT, TYPESENSE_PROTOCOL, TYPESENSE_API_KEY,
    PRODUCTS_COLLECTION, CATEGORIES_COLLECTION, CONV_MODEL_ID, PRICE_FIELD,
    EMBEDDING_MODEL, EMBEDDING_FIELDS, openai_client,
)

MULTI_ITEM_THRESHOLD = 2
MAX_EXPANSION_ITEMS  = 10   # cap sub-searches so we don't blow up multi_search payloads
TOTAL_HIT_BUDGET     = 10   # keep overall result size comparable to old single-search behaviour
MIN_HITS_PER_ITEM    = 1

CATEGORY_MATCHES_PER_ITEM   = 3
# Category-filtered results win unless their best vector_distance is worse
# than the unfiltered search's best by more than this. Measured on the live
# catalog: genuine category wins ("Butter" -> dairy, "Wallet") come out
# closer, while bad category guesses ("Laptop" -> Laptop Bags, "red chili
# powder" -> face Powders) come out 0.04+ further away.
CATEGORY_DISTANCE_TOLERANCE = 0.02


def embed_query_text(text: str) -> list[float]:
    """Embeds `text` with the same model PRODUCTS_COLLECTION's "embedding"
    field uses (see config.EMBEDDING_MODEL), so we can hand Typesense a raw
    vector via vector_query instead of letting it auto-embed "q" server-side
    — that server-side embedder is currently broken (see search.py module
    docstring / config.py comment), and fixing it would mean altering the
    collection schema, which we've been asked not to do."""
    return embed_query_texts([text])[0]


def embed_query_texts(texts: list[str]) -> list[list[float]]:
    """Batch form of embed_query_text — one OpenAI round trip for all items
    of a multi-item list instead of one per item."""
    resp = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


def _vector_query_clause(vector: list[float], k: int) -> str:
    """Builds a Typesense vector_query clause carrying our own precomputed
    vector for the "embedding" field. alpha:1 makes ranking purely
    vector-distance-based (matching the old query_by=embedding pure-semantic
    behaviour), since query_by below has to reference a real text field."""
    values = ",".join(f"{v:.8f}" for v in vector)
    return f"embedding:([{values}], k:{k}, alpha:1)"


def price_filter_to_filter_by(price_filter: dict | None) -> str | None:
    """Builds a Typesense filter_by clause for PRICE_FIELD from a
    {"min": float|None, "max": float|None} dict. Price is a plain indexed
    number, not one of the embedded/semantic fields — vector search can't
    reliably enforce an exact numeric threshold like "under 2500", so that
    constraint is enforced here instead, as a real filter."""
    if not price_filter:
        return None
    lo, hi = price_filter.get("min"), price_filter.get("max")
    if lo is not None and hi is not None:
        return f"{PRICE_FIELD}:[{int(round(lo))}..{int(round(hi))}]"
    if hi is not None:
        return f"{PRICE_FIELD}:<={int(round(hi))}"
    if lo is not None:
        return f"{PRICE_FIELD}:>={int(round(lo))}"
    return None


def _typesense_multi_search(body: dict, params: dict, conversation_id: str | None):
    """POST to Typesense /multi_search with timeout/connection handling shared
    by both the conversational and plain search calls."""
    base_url = f"{TYPESENSE_PROTOCOL}://{TYPESENSE_HOST}:{TYPESENSE_PORT}/multi_search"
    url = f"{base_url}?{urlencode(params)}"
    headers = {
        "Content-Type":        "application/json",
        "X-TYPESENSE-API-KEY": TYPESENSE_API_KEY,
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
    except requests.exceptions.Timeout:
        # A timeout here is a read timeout (server reachable, slow to respond) —
        # stripping conversation_id is a reasonable retry since a bloated/invalid
        # conversation context can occasionally slow that endpoint down.
        if conversation_id:
            params = dict(params)
            params.pop("conversation_id", None)
            url = f"{base_url}?{urlencode(params)}"
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=30)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e2:
                raise RuntimeError(f"Typesense request timed out. Please try again. ({e2})")
        else:
            raise RuntimeError("Typesense request timed out. Please try again.")
    except requests.exceptions.ConnectionError as e:
        # Could not even establish a connection (host unreachable, DNS failure,
        # cluster paused, firewall). Retrying without conversation_id won't fix
        # this, so fail fast with a clean message instead of a raw traceback.
        raise RuntimeError(
            f"Could not reach the search service. Please check that the "
            f"Typesense host is up and reachable. ({e})"
        )

    if not resp.ok and resp.status_code == 400 and conversation_id and "conversation_id" in resp.text.lower():
        # The client handed back a conversation_id Typesense no longer
        # recognizes (expired TTL, or the conversation model was deleted and
        # recreated since). Typesense rejects this outright rather than
        # silently starting fresh, so retry once as a brand-new conversation.
        params = dict(params)
        params.pop("conversation_id", None)
        url = f"{base_url}?{urlencode(params)}"
        resp = requests.post(url, headers=headers, json=body, timeout=30)

    if not resp.ok:
        raise RuntimeError(f"Typesense RAG error {resp.status_code}: {resp.text}")

    payload = resp.json()

    # A 200 at the multi_search level can still carry a per-search failure
    # (e.g. the embedding field's auto-embed model call to OpenAI erroring
    # out) — that shows up as {"code": ..., "error": ...} instead of "hits"
    # inside results[]. Left unchecked, callers silently read this as "zero
    # hits" and report "no products found", masking a real backend error.
    for r in payload.get("results", []):
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(f"Typesense search error {r.get('code')}: {r['error']}")

    return payload


def match_categories(items: list[str]) -> list[list[str]]:
    """Keyword-matches each item against CATEGORIES_COLLECTION and returns,
    per item, up to CATEGORY_MATCHES_PER_ITEM category ids (the values used
    in products' category_ids). prefix is off so "Tea" can't prefix-match
    "Team Sports"; plural forms ("Perfume" -> "Perfumes") still match via
    Typesense's default typo tolerance. Fails open to "no categories" on any
    error — the category step only refines results, it must never break
    search."""
    body = {"searches": [{
        "collection":       CATEGORIES_COLLECTION,
        "q":                item,
        "query_by":         "category_name,path",
        "query_by_weights": "3,1",
        "prefix":           "false",
        "filter_by":        "status:=1 && product_count:>0",
        "include_fields":   "id",
        "per_page":         CATEGORY_MATCHES_PER_ITEM * 2,
    } for item in items]}
    try:
        results = _typesense_multi_search(body, {}, None).get("results", [])
    except RuntimeError as e:
        print("-----category lookup failed, searching without categories: ", e)
        return [[] for _ in items]

    matches = []
    for r in results:
        hits = r.get("hits", [])
        if hits:
            # Keep only the categories matching as many query tokens as the
            # best one (e.g. "Dairy Butter" -> Dairy/Butter & Margarine, not
            # every category that merely contains "Dairy").
            best = max(h["text_match_info"]["tokens_matched"] for h in hits)
            hits = [h for h in hits if h["text_match_info"]["tokens_matched"] == best]
        matches.append([h["document"]["id"] for h in hits[:CATEGORY_MATCHES_PER_ITEM]])
    return matches


def _join_filters(*clauses: str | None) -> str | None:
    clauses = [c for c in clauses if c]
    return " && ".join(f"({c})" for c in clauses) if clauses else None


def _category_filter(category_ids: list[str]) -> str | None:
    return f"category_ids:[{','.join(category_ids)}]" if category_ids else None


def _product_search(vector: list[float], k: int, filter_by: str | None) -> dict:
    # q="*" + our own vector = pure vector search. Passing item text as q
    # makes Typesense auto-embed it server-side when query_by includes the
    # "embedding" field — one remote embed call per sub-search, which times
    # out ("Request timed out", 500).
    search = {
        "collection":     PRODUCTS_COLLECTION,
        "q":              "*",
        "query_by":       EMBEDDING_FIELDS,
        "vector_query":   _vector_query_clause(vector, k),
        "exclude_fields": "embedding",
        "per_page":       k,
        "prefix":         "false",
    }
    if filter_by:
        search["filter_by"] = filter_by
    return search


def _top_distance(result: dict | None) -> float:
    hits = (result or {}).get("hits") or []
    return hits[0].get("vector_distance", float("inf")) if hits else float("inf")


def _category_wins(unfiltered: dict, filtered: dict | None) -> bool:
    """True when the category-filtered results are at least about as close
    a match as the unfiltered ones — see CATEGORY_DISTANCE_TOLERANCE."""
    if filtered is None or not filtered.get("hits"):
        return False
    return _top_distance(filtered) <= _top_distance(unfiltered) + CATEGORY_DISTANCE_TOLERANCE


def _run_with_and_without_categories(items, vectors, category_ids, k, filter_by):
    """For each item runs the plain vector search and, when the item matched
    any categories, the same search restricted to them — all in a single
    multi_search. Returns per item (hits, used_category: bool)."""
    searches, has_cats = [], []
    for vector, cats in zip(vectors, category_ids):
        searches.append(_product_search(vector, k, filter_by))
        if cats:
            searches.append(_product_search(vector, k, _join_filters(filter_by, _category_filter(cats))))
        has_cats.append(bool(cats))

    results = _typesense_multi_search({"searches": searches}, {}, None).get("results", [])

    out, i = [], 0
    for item, cats in zip(items, has_cats):
        unfiltered = results[i]
        filtered = results[i + 1] if cats else None
        i += 2 if cats else 1
        use_cat = _category_wins(unfiltered, filtered)
        print(f"-----item {item!r}: category filter {'USED' if use_cat else 'skipped'} "
              f"(all={_top_distance(unfiltered):.3f}, cat={_top_distance(filtered):.3f})")
        out.append(((filtered if use_cat else unfiltered).get("hits", []), use_cat))
    return out


def typesense_search_products(expanded_query: str, conversation_id: str | None = None, price_filter: dict | None = None):
    """Product retrieval, given an already-expanded/classified query (see
    query_engine.analyze_query).

    Each item is first matched against CATEGORIES_COLLECTION; the product
    search is then restricted to those categories when that gives results at
    least as close as an unrestricted search (see _category_wins), so e.g.
    "Butter" stays in Dairy instead of returning body butter lotions.

    Returns (hits, conversation_result). conversation_result is
    {"answer": str, "conversation_id": str} for the single-item path, or
    None for the multi-item path (caller must generate its own answer)."""
    print("-----fresh search query to typesense: ", expanded_query)
    filter_by = price_filter_to_filter_by(price_filter)
    if filter_by:
        print("-----price filter_by: ", filter_by)

    items = [i.strip() for i in expanded_query.split(",") if i.strip()]
    is_multi_item = len(items) >= MULTI_ITEM_THRESHOLD

    if not is_multi_item:
        params = {
            "q":                     expanded_query,
            "conversation":          "true",
            "conversation_model_id": CONV_MODEL_ID,
        }
        if conversation_id:
            params["conversation_id"] = conversation_id

        vector = embed_query_text(expanded_query)
        search_filter = filter_by
        # Only fresh searches get a category: a follow-up's text ("for men's",
        # "show more") is a fragment Typesense rewrites from the conversation
        # history, so matching categories against it would be meaningless.
        if not conversation_id:
            cats = match_categories([expanded_query])[0]
            if cats:
                (_, use_cat), = _run_with_and_without_categories(
                    [expanded_query], [vector], [cats], TOTAL_HIT_BUDGET, filter_by)
                if use_cat:
                    search_filter = _join_filters(filter_by, _category_filter(cats))

        search = _product_search(vector, TOTAL_HIT_BUDGET, search_filter)
        # With conversation=true Typesense only accepts q as a URL param
        # (set in params above), never inside the search body.
        search.pop("q")
        data = _typesense_multi_search({"searches": [search]}, params, conversation_id)
        results = data.get("results", [])
        hits = results[0].get("hits", []) if results else []

        conv = data.get("conversation", {})
        conversation_result = {
            "answer":          conv.get("answer", ""),
            "conversation_id": conv.get("conversation_id") or conversation_id,
        }
        return hits, conversation_result

    # ── Multi-item path: one sub-search per item, round-robin + de-dupe ──
    items = items[:MAX_EXPANSION_ITEMS]
    per_item = max(MIN_HITS_PER_ITEM, TOTAL_HIT_BUDGET // len(items))
    print(f"-----multi-item expansion detected ({len(items)} items), {per_item} hits/item: {items}")

    vectors = embed_query_texts(items)
    category_ids = match_categories(items)
    per_item_hits = [h for h, _ in _run_with_and_without_categories(
        items, vectors, category_ids, per_item, filter_by)]

    hits = []
    seen_ids = set()
    max_len = max((len(h) for h in per_item_hits), default=0)
    for i in range(max_len):
        for item_hits in per_item_hits:
            if i >= len(item_hits):
                continue
            hit = item_hits[i]
            doc_id = hit.get("document", {}).get("id")
            if doc_id is not None:
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
            hits.append(hit)

    return hits, None
