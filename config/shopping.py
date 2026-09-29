"""shopping.py - Typesense / OpenAI settings and clients for the shopping domain.

Ported from npk_shopping_chatbot/config.py. query_engine.py and search.py
import from here instead of the old top-level `config` module.
"""

import os
from dotenv import load_dotenv
import typesense
from openai import OpenAI

load_dotenv()

# ─── Typesense ────────────────────────────────────────────────────────────────
TYPESENSE_HOST     = os.getenv("TYPESENSE_HOST", "localhost")
TYPESENSE_PORT     = int(os.getenv("TYPESENSE_PORT", 8108))
TYPESENSE_PROTOCOL = os.getenv("TYPESENSE_PROTOCOL", "http")
TYPESENSE_API_KEY  = os.getenv("TYPESENSE_API_KEY")

PRODUCTS_COLLECTION = os.getenv("TYPESENSE_PRODUCTS_COLLECTION", "magento_v1_default-products")
CONV_MODEL_ID        = os.getenv("TYPESENSE_CONV_MODEL_ID", "naheed-shopping-model")

# Must match the "embedding" field's model_config.model_name / num_dim on
# PRODUCTS_COLLECTION exactly (openai/text-embedding-3-small, 1536 dims) — see
# search.py::embed_query_text. Queries are embedded ourselves and the raw
# vector is passed via vector_query instead of relying on Typesense's
# server-side auto-embedder for that field.
EMBEDDING_MODEL = os.getenv("SHOPPING_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_FIELDS = os.getenv(
    "SHOPPING_EMBEDDING_FIELDS",
    "product_name,category,manufacturer,description,short_description",
)

# Numeric field on PRODUCTS_COLLECTION used for price filter_by (see
# search.py::price_filter_to_filter_by) — a plain indexed number, not one of
# the embedded/semantic fields.
PRICE_FIELD = os.getenv("PRICE_FIELD", "price")

VECTOR_DISTANCE_THRESHOLD = float(os.getenv("VECTOR_DISTANCE_THRESHOLD", "0.5"))

# ─── LLM (shopping's own query understanding / off-topic guard) ──────────────
LLM_API_KEY = os.getenv("SHOPPING_LLM_API_KEY")
LLM_MODEL   = os.getenv("SHOPPING_LLM_MODEL")

# ─── Clients ──────────────────────────────────────────────────────────────────
ts_client = typesense.Client({
    "nodes": [{
        "host":     TYPESENSE_HOST,
        "port":     str(TYPESENSE_PORT),
        "protocol": TYPESENSE_PROTOCOL,
    }],
    "api_key":                    TYPESENSE_API_KEY,
    "connection_timeout_seconds": 30,
})

openai_client = OpenAI(api_key=LLM_API_KEY)
