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
    PRODUCTS_COLLECTION, CONV_MODEL_ID, PRICE_FIELD,
    EMBEDDING_MODEL, EMBEDDING_FIELDS, openai_client,
)

MULTI_ITEM_THRESHOLD = 2
MAX_EXPANSION_ITEMS  = 10   # cap sub-searches so we don't blow up multi_search payloads
TOTAL_HIT_BUDGET     = 10   # keep overall result size comparable to old single-search behaviour
MIN_HITS_PER_ITEM    = 1


def embed_query_text(text: str) -> list[float]:
    """Embeds `text` with the same model PRODUCTS_COLLECTION's "embedding"
    field uses (see config.EMBEDDING_MODEL), so we can hand Typesense a raw
    vector via vector_query instead of letting it auto-embed "q" server-side
    — that server-side embedder is currently broken (see search.py module
    docstring / config.py comment), and fixing it would mean altering the
    collection schema, which we've been asked not to do."""
    resp = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


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


def typesense_search_products(expanded_query: str, conversation_id: str | None = None, price_filter: dict | None = None):
    """Product retrieval, given an already-expanded/classified query (see
    query_engine.analyze_query).

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
        search = {
            "collection":     PRODUCTS_COLLECTION,
            "query_by":       EMBEDDING_FIELDS,
            "vector_query":   _vector_query_clause(vector, TOTAL_HIT_BUDGET),
            "exclude_fields": "embedding",
            "per_page":       TOTAL_HIT_BUDGET,
            "prefix":         "false",
        }
        if filter_by:
            search["filter_by"] = filter_by

        body = {"searches": [search]}
        data = _typesense_multi_search(body, params, conversation_id)
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

    def _search_for(item):
        vector = embed_query_text(item)
        search = {
            "collection":       PRODUCTS_COLLECTION,
            "q":                item,
            "query_by":         EMBEDDING_FIELDS,
            "vector_query":     _vector_query_clause(vector, per_item),
            "exclude_fields":   "embedding",
            "per_page":         per_item,
            "prefix":           "false",
        }
        if filter_by:
            search["filter_by"] = filter_by
        return search

    body = {"searches": [_search_for(item) for item in items]}
    data = _typesense_multi_search(body, {}, None)
    results = data.get("results", [])

    hits = []
    per_item_hits = [r.get("hits", []) for r in results]
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
