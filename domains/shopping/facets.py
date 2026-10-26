"""
facets.py – Maps the brand / category a customer asked for onto the exact
`manufacturer` / `category` values stored in PRODUCTS_COLLECTION, so they can
be used as a Typesense filter_by (see search.py).

Customers rarely spell these the way the catalog does ("loreal" for
"L'Oreal Paris", "nivia" for "Nivea", "mens perfume" for "Men Perfumes"), so
the hints are matched fuzzily against the live facet values. Those values
are read once with a plain facet search and cached for FACET_CACHE_SECONDS —
read-only, nothing on Typesense is ever changed.

Everything here fails open: an unreachable Typesense or an unmatched hint
just means "no filter", never a broken search. category_ids is deliberately
not used.
"""

import html
import re
import threading
import time
from difflib import SequenceMatcher

import requests

from config.shopping import (
    FILTER_BY_CATEGORY,
    FILTER_BY_MANUFACTURER,
    PRODUCTS_COLLECTION,
    TYPESENSE_API_KEY,
    TYPESENSE_HOST,
    TYPESENSE_PORT,
    TYPESENSE_PROTOCOL,
)

MANUFACTURER_FIELD = "manufacturer"
CATEGORY_FIELD = "category"

# Filters switched on in config (SHOPPING_FILTER_BY_MANUFACTURER /
# SHOPPING_FILTER_BY_CATEGORY); a switched-off field is never loaded or used.
ENABLED_FIELDS = [
    f for f, on in ((MANUFACTURER_FIELD, FILTER_BY_MANUFACTURER), (CATEGORY_FIELD, FILTER_BY_CATEGORY)) if on
]

FACET_CACHE_SECONDS = 6 * 3600
MAX_FACET_VALUES = 10000
MAX_MATCHES = 3

# Brand names are short and often misspelled, so they get a looser cutoff
# than categories, whose hint is already English written by the model.
BRAND_MIN_SCORE = 0.8
CATEGORY_MIN_SCORE = 0.88
# Other matches are kept only when about as good as the best one.
MATCH_SCORE_MARGIN = 0.03

# Placeholder brands that never narrow anything down.
_IGNORED_BRANDS = {"unbranded", "generic", "other", "others", "na", "none"}

_cache = {"at": 0.0, "values": {}}
_lock = threading.Lock()


def _normalize(text: str) -> list[str]:
    """Lowercase tokens, singular, without punctuation: "Men's Perfumes" ->
    ["men", "perfume"]. Catalog values carry HTML entities ("Watches&#44;
    Bags & Jewellery"), so they are unescaped first."""
    text = html.unescape(str(text)).lower().replace("&", " and ")
    text = re.sub(r"'s\b", "", text)
    tokens = re.findall(r"[a-z0-9]+", text.replace("'", ""))
    out = []
    for t in tokens:
        if t == "and":
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("es") and t[-3] in "sxz":
            t = t[:-2]
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def _load_values() -> dict[str, list[str]]:
    params = {
        "q": "*",
        "query_by": "product_name",
        "per_page": 0,
        "facet_by": ",".join(ENABLED_FIELDS),
        "max_facet_values": MAX_FACET_VALUES,
    }
    url = (
        f"{TYPESENSE_PROTOCOL}://{TYPESENSE_HOST}:{TYPESENSE_PORT}"
        f"/collections/{PRODUCTS_COLLECTION}/documents/search"
    )
    resp = requests.get(
        url, headers={"X-TYPESENSE-API-KEY": TYPESENSE_API_KEY}, params=params, timeout=30
    )
    resp.raise_for_status()
    values = {}
    for fc in resp.json().get("facet_counts", []):
        # A backtick can't be escaped inside a filter_by value list.
        values[fc["field_name"]] = [
            c["value"] for c in fc.get("counts", []) if c.get("value") and "`" not in c["value"]
        ]
    return values


def _values(field: str) -> list[str]:
    if field not in ENABLED_FIELDS:
        return []
    with _lock:
        if time.time() - _cache["at"] > FACET_CACHE_SECONDS or not _cache["values"]:
            try:
                _cache["values"] = _load_values()
                print(
                    "-----facet values loaded: "
                    + ", ".join(f"{k}={len(v)}" for k, v in _cache["values"].items())
                )
            except Exception as e:
                print(f"-----facet values could not be loaded, searching without filters: {e}")
            # Also on failure, so a down Typesense isn't hit again every turn.
            _cache["at"] = time.time()
        return _cache["values"].get(field, [])


def _score(hint_tokens: list[str], value_tokens: list[str], loose: bool) -> float:
    if not hint_tokens or not value_tokens:
        return 0.0
    hint, value = "".join(hint_tokens), "".join(value_tokens)
    if hint == value or set(hint_tokens) == set(value_tokens):
        return 1.0
    score = SequenceMatcher(None, hint, value).ratio()
    if loose:
        # Brands are often given without their suffix ("maybelline" for
        # "Maybelline New York", "loreal" for "L'Oreal Paris"): also compare
        # against the leading part of the value, misspelling included.
        if len(hint) >= 4 and value.startswith(hint):
            score = max(score, 0.97)
        head = value[: len(hint) + 1]
        if len(hint) >= 4:
            score = max(score, SequenceMatcher(None, hint, head).ratio() - 0.02)
    return score


def _match(hint, field: str, min_score: float, loose: bool) -> list[str]:
    hint_tokens = _normalize(hint or "")
    if not hint_tokens or "".join(hint_tokens) in _IGNORED_BRANDS:
        return []
    scored = []
    for value in _values(field):
        if loose and "".join(_normalize(value)) in _IGNORED_BRANDS:
            continue
        s = _score(hint_tokens, _normalize(value), loose)
        if s >= min_score:
            scored.append((s, value))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0]
    return [v for s, v in scored if s >= best - MATCH_SCORE_MARGIN][:MAX_MATCHES]


def match_brands(hint) -> list[str]:
    """Catalog manufacturer values for a customer-spelled brand ([] = no
    confident match)."""
    return _match(hint, MANUFACTURER_FIELD, BRAND_MIN_SCORE, loose=True)


def match_categories(hint) -> list[str]:
    """Catalog category values for an English category hint ([] = no
    confident match)."""
    return _match(hint, CATEGORY_FIELD, CATEGORY_MIN_SCORE, loose=False)


def resolve(brand=None, category=None) -> dict[str, list[str]]:
    """{"manufacturer": [...], "category": [...]} for whichever hints matched
    a catalog value; fields with no match, or switched off in config, are
    left out."""
    brand = brand if MANUFACTURER_FIELD in ENABLED_FIELDS else None
    category = category if CATEGORY_FIELD in ENABLED_FIELDS else None
    resolved = {}
    brands = match_brands(brand) if brand else []
    if brands:
        resolved[MANUFACTURER_FIELD] = brands
    categories = match_categories(category) if category else []
    if categories:
        resolved[CATEGORY_FIELD] = categories
    if brand or category:
        print(f"-----facet hints brand={brand!r} category={category!r} -> {resolved}")
    return resolved


def filter_chain(resolved: dict[str, list[str]]) -> list[dict[str, list[str]]]:
    """The filters to try, most specific first. The brand is what the
    customer explicitly named, so when brand + category together find
    nothing, the brand alone is tried before dropping both."""
    resolved = {f: v for f, v in resolved.items() if v}
    chain = [resolved] if resolved else []
    if len(resolved) > 1 and MANUFACTURER_FIELD in resolved:
        chain.append({MANUFACTURER_FIELD: resolved[MANUFACTURER_FIELD]})
    return chain


def to_filter_by(resolved: dict[str, list[str]]) -> str | None:
    clauses = [
        f"{field}:=[{','.join(f'`{v}`' for v in values)}]"
        for field, values in resolved.items()
        if values
    ]
    return " && ".join(clauses) if clauses else None
