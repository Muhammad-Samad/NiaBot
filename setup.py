"""
setup.py – Run ONCE before starting the app.
Creates:
  1. conversation_store collection in Typesense
  2. Groq-backed conversation model in Typesense
"""

import os
import sys
import typesense
from dotenv import load_dotenv

load_dotenv()

TYPESENSE_HOST     = os.getenv("TYPESENSE_HOST", "localhost")
TYPESENSE_PORT     = int(os.getenv("TYPESENSE_PORT", 8108))
TYPESENSE_PROTOCOL = os.getenv("TYPESENSE_PROTOCOL", "http")
TYPESENSE_API_KEY  = os.getenv("TYPESENSE_API_KEY")
LLM_API_KEY       = os.getenv("SHOPPING_LLM_API_KEY")
LLM_MODEL         = os.getenv("SHOPPING_LLM_MODEL", "gpt-3.5-turbo")




if not TYPESENSE_API_KEY:
    sys.exit("ERROR: TYPESENSE_API_KEY not set in .env")
if not LLM_API_KEY:
    sys.exit("ERROR: LLM_API_KEY not set in .env")

client = typesense.Client({
    "nodes": [{
        "host":     TYPESENSE_HOST,
        "port":     str(TYPESENSE_PORT),
        "protocol": TYPESENSE_PROTOCOL,
    }],
    "api_key":           TYPESENSE_API_KEY,
    "connection_timeout_seconds": 10,
})

# --- 1. conversation_store collection -----------------------------------------
CONVERSATION_STORE_SCHEMA = {
    "name": "conversation_store",
    "fields": [
        {"name": "conversation_id", "type": "string"},
        {"name": "model_id",        "type": "string"},
        {"name": "timestamp",       "type": "int32"},
        {"name": "role",            "type": "string", "index": False},
        {"name": "message",         "type": "string", "index": False},
    ],
}

print("\n[1/2] Creating 'conversation_store' collection ...")
try:
    existing = [c["name"] for c in client.collections.retrieve()]
    if "conversation_store" in existing:
        print("      Already exists - skipping.")
    else:
        client.collections.create(CONVERSATION_STORE_SCHEMA)
        print("      Created successfully.")
except Exception as e:
    print(f"      Failed: {e}")
    sys.exit(1)

# --- 2. Conversation model ----------------------------------------------------


# This is the system prompt for Typesense's own conversation model, used by
# app.py's single-item search path via the conversation=true + conversation_id
# "Follow-up Questions" feature. Typesense uses it for two jobs: (1) rewriting
# a follow-up fragment ("for men's", "under 500") into a standalone search
# query using the real conversation_store history, and (2) drafting the reply
# text grounded in whether hits were found.
#
# Off-topic gating and query expansion now happen in app.py BEFORE this is
# ever reached (see query_engine.analyze_query) — that's the primary gate, so
# most off-topic messages never make it to Typesense at all. The "don't
# entertain non-shopping queries" clause below is kept anyway as a defense-in-
# depth fallback for whatever slips through (e.g. a jailbreak attempt, or a
# borderline case our classifier let past). It should read consistently with
# query_engine.build_multi_item_answer's rules for the multi-item path — both
# forbid narrating product names/prices, since the UI renders product cards
# separately.
SYSTEM_PROMPT = """
You are NiaBot (Naheed Intelligent Assistant), a friendly shopping assistant for Naheed.pk.
If a message reaches you that is clearly unrelated to shopping (chit-chat, weather, politics, coding help), gently redirect the customer back to shopping instead of answering it.

Guidelines:
* Use the search results ONLY to determine whether relevant products exist.
* NEVER mention product names, brands, prices, SKUs, specifications, or availability from the search results in your response. The matching products are displayed separately in the UI.
* Respond only to the customer's request in a natural, conversational way.
* If matching products exist, simply acknowledge that you found suitable options and invite the customer to choose.
* If no matches are found, clearly say so and suggest the closest related product category.
* Preserve all requirements stated in the customer's message (gender, product type, brand, flavor, size, weight, color, intended use).
* Use prior conversation turns only to understand follow-up context (e.g. "for men's", "only white ones", "show more") — never to restate or re-list earlier products.
* Keep responses concise (1–2 sentences).
"""

CONVERSATION_MODEL = {
    "id":                 "naheed-shopping-model",
    "model_name":         f"openai/{LLM_MODEL}",
    "api_key":            LLM_API_KEY,
    # "openai_url":         "https://api.groq.com/openai",
    "history_collection": "conversation_store",
    "max_bytes":          16384,
    "ttl":                86400,
    "system_prompt":      SYSTEM_PROMPT,
}

print("\n[2/2] Creating conversation model 'naheed-shopping-model' ...")
import requests, json

headers = {
    "Content-Type":       "application/json",
    "X-TYPESENSE-API-KEY": TYPESENSE_API_KEY,
}
url = f"{TYPESENSE_PROTOCOL}://{TYPESENSE_HOST}:{TYPESENSE_PORT}/conversations/models"

try:
    r = requests.get(url, headers=headers, timeout=10)
    models = r.json() if r.ok else []
    existing_ids = [m.get("id") for m in (models if isinstance(models, list) else [])]
    if "naheed-shopping-model" in existing_ids:
        print("      Model already exists - skipping.")
    else:
        r = requests.post(url, headers=headers, json=CONVERSATION_MODEL, timeout=10)
        if r.ok:
            print("      Model created successfully.")
            print(f"      Model ID: {r.json().get('id')}")
        else:
            print(f"      Failed: {r.status_code} {r.text}")
            sys.exit(1)
except Exception as e:
    print(f"      Error: {e}")
    sys.exit(1)

print("\nSetup complete! You can now run: python app.py\n")