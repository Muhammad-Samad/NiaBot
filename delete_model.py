import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TYPESENSE_HOST     = os.getenv("TYPESENSE_HOST", "localhost")
TYPESENSE_PORT     = int(os.getenv("TYPESENSE_PORT", 8108))
TYPESENSE_PROTOCOL = os.getenv("TYPESENSE_PROTOCOL", "http")
TYPESENSE_API_KEY  = os.getenv("TYPESENSE_API_KEY")

if not TYPESENSE_API_KEY:
    sys.exit("ERROR: TYPESENSE_API_KEY not set in .env")

r = requests.delete(
    f"{TYPESENSE_PROTOCOL}://{TYPESENSE_HOST}:{TYPESENSE_PORT}/conversations/models/naheed-shopping-model",
    headers={"X-TYPESENSE-API-KEY": TYPESENSE_API_KEY},
)
print(r.status_code, r.text)