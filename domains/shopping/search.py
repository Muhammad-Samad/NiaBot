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
    path (see reply.py).

Price constraints ("under 2500") are applied as a Typesense filter_by on the
plain numeric PRICE_FIELD, never through the embedding — see
price_filter_to_filter_by().
"""

from urllib.parse import urlencode

import requests

from config.shopping import (
    TYPESENSE_HOST,
    TYPESENSE_PORT,
    TYPESENSE_PROTOCOL,
    TYPESENSE_API_KEY,
    PRODUCTS_COLLECTION,
    CONV_MODEL_ID,
    PRICE_FIELD,
    EMBEDDING_MODEL,
    EMBEDDING_FIELDS,
    VECTOR_DISTANCE_THRESHOLD,
    openai_client,
)

MULTI_ITEM_THRESHOLD = 2
MAX_EXPANSION_ITEMS = 8  # cap sub-searches so we don't blow up multi_search payloads
TOTAL_HIT_BUDGET = (
    8  # keep overall result size comparable to old single-search behaviour
)
MIN_HITS_PER_ITEM = 1

# Key added to every returned hit: the searched items it was found for (one
# product can match several), so the caller can tell which items came back
# empty (see reply.py).
MATCHED_ITEM = "_matched_item"

# Cheapest-first (budget requests): fetch this many times more candidates
# than will be shown, keep only the ones about as relevant as the best match
# — within VECTOR_DISTANCE_THRESHOLD and within CHEAPEST_RELEVANCE_MARGIN of
# the item's best distance, so a cheap but loosely related product ("Cooking
# Oil" -> oil spray can) can't win on price alone — then sort by price.
CHEAPEST_CANDIDATE_MULTIPLIER = 4
CHEAPEST_MIN_CANDIDATES = (
    12  # a long list gets 1 hit/item — still compare a real price range
)
CHEAPEST_RELEVANCE_MARGIN = 0.06


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
        "Content-Type": "application/json",
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
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as e2:
                raise RuntimeError(
                    f"Typesense request timed out. Please try again. ({e2})"
                )
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

    if (
        not resp.ok
        and resp.status_code == 400
        and conversation_id
        and "conversation_id" in resp.text.lower()
    ):
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


def _product_search(vector: list[float], k: int, filter_by: str | None) -> dict:
    # q="*" + our own vector = pure vector search. Passing item text as q
    # makes Typesense auto-embed it server-side when query_by includes the
    # "embedding" field — one remote embed call per sub-search, which times
    # out ("Request timed out", 500).
    search = {
        "collection": PRODUCTS_COLLECTION,
        "q": "*",
        "query_by": EMBEDDING_FIELDS,
        "vector_query": _vector_query_clause(vector, k),
        "exclude_fields": "embedding",
        "per_page": k,
        "prefix": "false",
    }
    if filter_by:
        search["filter_by"] = filter_by
    return search


def _price_of(hit) -> float:
    try:
        return float(hit.get("document", {}).get(PRICE_FIELD) or 0)
    except (TypeError, ValueError):
        return 0.0


def _cheapest_relevant(hits: list, n: int) -> list:
    """The n cheapest of the hits that are about as relevant as the best one
    (see CHEAPEST_RELEVANCE_MARGIN). Price-0 products (missing catalog
    price) go last, so they don't crowd out real cheap options."""
    if not hits:
        return hits
    best = min(h.get("vector_distance", float("inf")) for h in hits)
    cutoff = min(VECTOR_DISTANCE_THRESHOLD, best + CHEAPEST_RELEVANCE_MARGIN)
    relevant = [h for h in hits if h.get("vector_distance", 0) <= cutoff] or hits[:n]
    return sorted(relevant, key=lambda h: (_price_of(h) <= 0, _price_of(h)))[:n]


def typesense_search_products(
    expanded_query: str,
    conversation_id: str | None = None,
    price_filter: dict | None = None,
    cheapest_first: bool = False,
):
    """Product retrieval, given an already-expanded/classified query (see
    query_engine.analyze_query).

    cheapest_first (budget requests) returns, per item, the cheapest of the
    closely relevant matches instead of the closest ones — see
    _cheapest_relevant().

    Returns (hits, conversation_result). conversation_result is
    {"answer": str, "conversation_id": str} for the single-item path, or
    None for the multi-item path (caller must generate its own answer)."""
    print(
        "-----fresh search query to typesense: ",
        expanded_query,
        "(cheapest first)" if cheapest_first else "",
    )
    filter_by = price_filter_to_filter_by(price_filter)
    if filter_by:
        print("-----price filter_by: ", filter_by)

    items = [i.strip() for i in expanded_query.split(",") if i.strip()]
    is_multi_item = len(items) >= MULTI_ITEM_THRESHOLD

    if not is_multi_item:
        params = {
            "q": expanded_query,
            "conversation": "true",
            "conversation_model_id": CONV_MODEL_ID,
        }
        if conversation_id:
            params["conversation_id"] = conversation_id

        vector = embed_query_text(expanded_query)

        k = (
            TOTAL_HIT_BUDGET * CHEAPEST_CANDIDATE_MULTIPLIER
            if cheapest_first
            else TOTAL_HIT_BUDGET
        )
        search = _product_search(vector, k, filter_by)
        # With conversation=true Typesense only accepts q as a URL param
        # (set in params above), never inside the search body.
        search.pop("q")
        data = _typesense_multi_search({"searches": [search]}, params, conversation_id)
        results = data.get("results", [])
        hits = results[0].get("hits", []) if results else []
        if cheapest_first:
            hits = _cheapest_relevant(hits, TOTAL_HIT_BUDGET)
        for hit in hits:
            hit[MATCHED_ITEM] = [expanded_query]

        conv = data.get("conversation", {})
        conversation_result = {
            "answer": conv.get("answer", ""),
            "conversation_id": conv.get("conversation_id") or conversation_id,
        }
        return hits, conversation_result

    # ── Multi-item path: one sub-search per item, round-robin + de-dupe ──
    items = items[:MAX_EXPANSION_ITEMS]
    per_item = max(MIN_HITS_PER_ITEM, TOTAL_HIT_BUDGET // len(items))
    print(
        f"-----multi-item expansion detected ({len(items)} items), {per_item} hits/item: {items}"
    )

    vectors = embed_query_texts(items)
    fetch_k = (
        max(per_item * CHEAPEST_CANDIDATE_MULTIPLIER, CHEAPEST_MIN_CANDIDATES)
        if cheapest_first
        else per_item
    )
    results = _typesense_multi_search(
        {"searches": [_product_search(v, fetch_k, filter_by) for v in vectors]}, {}, None
    ).get("results", [])
    per_item_hits = [r.get("hits", []) for r in results]
    if cheapest_first:
        per_item_hits = [_cheapest_relevant(h, per_item) for h in per_item_hits]
    for item, item_hits in zip(items, per_item_hits):
        for hit in item_hits:
            hit[MATCHED_ITEM] = [item]

    hits = []
    kept_by_id = {}
    max_len = max((len(h) for h in per_item_hits), default=0)
    for i in range(max_len):
        for item_hits in per_item_hits:
            if i >= len(item_hits):
                continue
            hit = item_hits[i]
            doc_id = hit.get("document", {}).get("id")
            if doc_id is not None:
                kept = kept_by_id.get(doc_id)
                if kept is not None:
                    # Same product found for another item too — remember that.
                    kept[MATCHED_ITEM] += [
                        m for m in hit[MATCHED_ITEM] if m not in kept[MATCHED_ITEM]
                    ]
                    continue
                kept_by_id[doc_id] = hit
            hits.append(hit)

    return hits, None
