# npk_combine_chatbot — NiaBot

**NiaBot** (Naheed Intelligent Assistant) is the customer-facing name of this
assistant. The name lives in `config/base.py` (`BOT_NAME`, `BOT_FULL_NAME`).

A single Flask app that unifies two previously separate bots into one
conversation:

- **Shopping** (ported from `npk_shopping_chatbot`) — Typesense-backed
  product search, cart, and order placement.
- **Operations** (ported from `npk_operation_chatbot`) — order tracking,
  complaints, refunds, order modification/cancellation, and general support,
  via an LLM-based intent classifier with a rule-based fallback.

Every chat message comes in through one endpoint (`/api/chat`). A domain
router (`router/domain_router.py`) decides per message whether the shopping
or operations logic should handle it, so the two bots behave as one
assistant rather than two tools glued together. See the "How it's wired
together" section below for details.

## Requirements

- Python 3.11+ (the operations code uses `str | None` type hints, which need
  3.10+)
- A MySQL database containing **both** schemas:
  - the shopping schema: `cart_sessions`, `cart_items`, `orders`,
    `order_items`, `conversation_messages` (see
    `../npk_shopping_chatbot/SQL_SETUP.sql` for the DDL if you need to
    (re)create these tables)
  - the Magento order-tracking schema the operations flows query
    (`sales_order`, `sales_order_item`, `sales_order_address`,
    `sales_order_payment`, `sales_creditmemo`, `nhd_sales_order_additionals`,
    `nhd_complain_tickets`, etc.)
- A Typesense instance with your products collection already indexed
  (unchanged from `npk_shopping_chatbot`)
- API keys for whichever LLM providers you use (OpenAI is required for the
  shopping domain's query understanding + embeddings; the operations domain
  can use OpenAI, Gemini, and/or Groq with automatic failover)

## Setup

### 1. Create and activate a virtual environment

From inside `npk_combine_chatbot/`:

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (cmd.exe):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

You should see `(.venv)` in your prompt once it's active.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example file and fill in real values:

```bash
cp .env.example .env
```

Then edit `.env` with your actual database credentials, Typesense host/API
key, and LLM API keys. Every variable in `.env.example` is documented inline
— in particular:

- `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` — the **one**
  shared database both domains read and write.
- `TYPESENSE_*` — your Typesense cluster and products collection.
- `SHOPPING_LLM_API_KEY` — must be a real OpenAI key (used for both chat
  completions and embeddings in the shopping domain).
- `OPENAI_API_KEY` / `GEMINI_API_KEY` / `GROQ_API_KEY` — operations domain
  LLM providers, tried in the order given by `LLM_PROVIDER_CHAIN`.

### 4. Run it

```bash
python app.py
```

By default this starts the Flask dev server on `http://localhost:5000`
(`FLASK_PORT` in `.env` to change the port, `FLASK_DEBUG=true` for
auto-reload + debugger). Open that URL in a browser to use the chat UI.

There is no Docker setup yet — this project is meant to be run and verified
locally first. Containerizing it is a later step.

## How it's wired together

```
npk_combine_chatbot/
├── app.py                    # Flask app: registers blueprints, owns the
│                              # single unified /api/chat endpoint
├── config/
│   ├── base.py                # Flask secret/debug/port, loads .env
│   ├── database.py            # single shared MySQL connection settings
│   └── shopping.py            # Typesense/OpenAI settings for shopping
├── router/
│   └── domain_router.py       # per-message shopping vs operations decision
├── domains/
│   ├── shopping/
│   │   ├── routes.py           # /api/cart/*, /api/order/place, /api/history
│   │   ├── service.py          # chat handling logic (was app.py's /api/chat)
│   │   ├── repository.py       # cart/order/message persistence
│   │   ├── query_engine.py     # off-topic guard, price parsing, LLM query expansion
│   │   └── search.py           # Typesense product search
│   └── operations/
│       ├── routes.py           # /api/upload (complaint image attachments)
│       └── service.py          # thin wrapper around core.ConversationManager
├── core/ flows/ services/ ai/ data/ database/ utils/
│                              # operations business logic, ported ~as-is from
│                              # npk_operation_chatbot (it was already
│                              # framework-agnostic — its Streamlit console and
│                              # FastAPI wrapper were both thin callers of the
│                              # same ConversationManager this app now calls
│                              # too)
├── templates/index.html       # single chat UI (shopping's UI, extended with
│                              # a working cart sidebar and an image-attach
│                              # button for the complaint flow)
└── static/uploads/            # complaint image attachments land here
```

**Why `core/`, `flows/`, `services/`, `ai/`, `data/`, `database/`, `utils/`
sit at the project root instead of under `domains/operations/`:** that code
uses absolute imports like `from services.order_service import OrderService`
and `from database.connection import DatabaseManager` internally (dozens of
call sites across the operations codebase). Keeping those packages at the
root, exactly as they were in `npk_operation_chatbot`, means every one of
those imports keeps working unchanged — the only file that needed editing
was `database/connection.py`, to point at the new shared `config/database.py`
instead of the old `config/settings.py`.

**Routing logic:** `router/domain_router.py` sends a message to the
operations domain if either (a) the session is already mid-flow there (e.g.
order tracking is waiting for an order ID — tracked via operations' own
`StateManager`), or (b) the message matches operational keywords/patterns
(order tracking/modify/cancel, refund, complaint, urgent delivery, a bare
order number, "talk to a human", a greeting/goodbye). Everything else
defaults to the shopping domain, which owns its own off-topic guard for pure
chit-chat. Once handed to operations, its own LLM-based intent classifier
does the real, nuanced intent resolution — the router's job is only the
shopping-vs-operations gate.

**Shared session:** the browser mints one `sid` (localStorage) that's sent
with every `/api/chat` call and used as the identity key for *both* domains
— shopping's cart/conversation state and operations' `StateManager` state.
This is what lets a user move from "show me shoes" to "track my order" in
the same chat thread. Every turn, regardless of which domain handled it, is
persisted to the same `conversation_messages` table, so `/api/history`
restores the full mixed conversation on page reload.

## Known items from the source projects worth knowing about

- `npk_shopping_chatbot`'s `.gitignore` file currently contains what look
  like real credential values instead of ignore patterns — worth checking
  that project's git history for leaked secrets and rotating any keys that
  may have been committed. This new project's `.gitignore` is a normal one.
- The original shopping bot's chat UI referenced a cart sidebar in its CSS
  and JavaScript that was never actually present in the page's HTML (the
  cart button did nothing). This project's `templates/index.html` includes
  the missing markup so the cart sidebar actually works.
