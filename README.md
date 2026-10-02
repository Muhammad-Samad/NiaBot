# npk_combine_chatbot — NiaBot

**NiaBot** (Naheed Intelligent Assistant) is the customer-facing name of this
assistant. The name lives in `config/base.py` (`BOT_NAME`, `BOT_FULL_NAME`).

A single Flask app that unifies three previously separate bots into one
conversation:

- **Shopping** (ported from `npk_shopping_chatbot`) — Typesense-backed
  product search, cart, and order placement.
- **Operations** (ported from `npk_operation_chatbot`) — order tracking,
  complaints, refunds, order modification/cancellation, and general support,
  via an LLM-based intent classifier with a rule-based fallback.
- **Policy** (ported from `General_Policy RAG`) — policy, FAQ and
  store-information answers (returns, refunds, delivery & shipping, pickup,
  payment methods, loyalty points, OTP/account, privacy, warranty, brands,
  company info), retrieved from a ChromaDB index of the Naheed knowledge base
  and answered by `gpt-4o-mini`.

Every chat message comes in through one endpoint (`/api/chat`). A domain
router (`router/domain_router.py`) decides per message whether the shopping,
operations or policy logic should handle it, so the bots behave as one
assistant rather than separate tools glued together. See the "How it's wired
together" section below for details.

## Requirements

- Python 3.11+ (the operations code uses `str | None` type hints, which need
  3.10+)
- A MySQL database containing **both** schemas:
  - the shopping schema: `cart_sessions`, `cart_items`, `orders`,
    `order_items`, `conversation_messages`, plus `conversation_sessions` /
    `chatbot_messages` (see `SQL_SETUP.sql` for the DDL; existing installs
    must run `migrations/add_policy_domain_columns.sql` — see "Policy
    domain" below)
  - the Magento order-tracking schema the operations flows query
    (`sales_order`, `sales_order_item`, `sales_order_address`,
    `sales_order_payment`, `sales_creditmemo`, `nhd_sales_order_additionals`,
    `nhd_complain_tickets`, etc.)
- A Typesense instance with your products collection already indexed
  (unchanged from `npk_shopping_chatbot`)
- API keys for whichever LLM providers you use (OpenAI is required for the
  shopping domain's query understanding + embeddings and for the policy
  domain; the operations domain can use OpenAI, Gemini, and/or Groq with
  automatic failover)

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
- `POLICY_*` — policy domain (RAG) settings; see "Policy domain" below.
  `POLICY_OPENAI_API_KEY` can be left empty to reuse `OPENAI_API_KEY`.
- `LOG_LEVEL` — `INFO` by default; `DEBUG` turns on diagnostic logs.

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
│   ├── shopping.py            # Typesense/OpenAI settings for shopping
│   └── policy.py              # RAG settings for policy (POLICY_* env vars)
├── router/
│   ├── domain_router.py       # per-message shopping / operations / policy decision
│   └── menu.py                # numbered main menu + Policy & FAQ sub-menu
├── domains/
│   ├── shopping/
│   │   ├── routes.py           # /api/cart/*, /api/order/place, /api/history
│   │   ├── service.py          # chat handling logic (was app.py's /api/chat)
│   │   ├── repository.py       # cart/order/message persistence
│   │   ├── query_engine.py     # off-topic guard, price parsing, LLM query expansion
│   │   └── search.py           # Typesense product search
│   ├── operations/
│   │   ├── routes.py           # /api/upload (complaint image attachments)
│   │   └── service.py          # thin wrapper around core.ConversationManager
│   └── policy/
│       ├── routes.py           # /api/policy/health (ChromaDB index status)
│       ├── service.py          # RAG answering + persistence of policy turns
│       ├── intent.py           # looks_like_policy(): regex policy detector
│       ├── rag/                # General_Policy RAG pipeline (copied as-is,
│       │                        # only import paths changed)
│       ├── knowledge/          # source knowledge-base document (.docx)
│       └── chroma/             # built ChromaDB index (committed)
├── scripts/ingest_policy.py   # (re)build the policy ChromaDB index
├── migrations/                # SQL to run by hand on existing databases
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

**Routing logic:** `router/domain_router.py` checks, in order:

1. **Menus** — "menu"/"help"/"options", and numbered menu picks. Main-menu
   flows go to operations; a pick from the **Policy & FAQ** sub-menu goes to
   the policy domain (each topic is sent to the RAG as a question).
2. **Policy questions** — `domains/policy/intent.py`'s `looks_like_policy()`
   (no LLM call): a policy/information topic (return policy, delivery
   charges, express shipping, roadside pickup, payment methods, loyalty
   points, OTP, privacy, warranty, "which brands…", outlets, contact…),
   unless the message is about the customer's *own* order or asks for an
   operations action ("I want to cancel / return…", "track…", complaints,
   damaged/expired items, an order number). Explicitly asking for a
   "policy" always counts.

   Before this step, if an operations flow is waiting for an **order ID or
   phone number** and the reply contains no digits (and isn't "cancel"), the
   customer has moved on: the flow is reset and the message is routed as a
   brand-new one — to shopping, policy or operations. The same happens when a
   Policy & FAQ menu topic is picked at that point.
3. **Operations mid-flow** — the session is in the middle of an operations
   flow (tracked via operations' own `StateManager`).
4. **Operational patterns** — order tracking/modify/cancel, refund,
   complaint, urgent delivery, a bare order number, "talk to a human", a
   greeting/goodbye. Operations' own LLM intent classifier then does the
   nuanced resolution; if it still says `general_policy`, operations hands
   the message to the policy domain (its `policy_rag` tool).
5. **Shopping** — everything else. If shopping's off-topic guard rejects the
   message, the policy domain answers it instead (it covers store questions
   the patterns missed and politely declines anything unrelated to Naheed).

**Switching mid-flow:** because step 2 runs before step 3, a policy question
asked in the middle of an operations flow (e.g. while a complaint is
collecting details — past the order ID / phone steps, which reset as
described above) is answered by the policy domain *without* touching the
operations state. The answer ends with "Now, back to your complaint - here's
where we left off: ..." repeating the last question, so the customer's next
reply simply continues the flow (or they type `cancel` to stop). A policy
turn also leaves shopping's state (`last_topic`, Typesense conversation id)
alone, so "show me cheaper ones" still works right after a policy question.

Every routing decision is logged once, e.g.
`Routed to policy (reason=policy_question_mid_flow, sid=...)`.

**Shared session:** the browser mints one `sid` (localStorage) that's sent
with every `/api/chat` call and used as the identity key for *both* domains
— shopping's cart/conversation state and operations' `StateManager` state.
This is what lets a user move from "show me shoes" to "track my order" in
the same chat thread. Every turn, regardless of which domain handled it, is
persisted to the same `conversation_messages` table (with a `domain`
column: `shopping` / `operations` / `policy` / `menu`), so `/api/history`
restores the full mixed conversation on page reload.

## Policy domain

The policy domain is the `General_Policy RAG` project plugged in as a
domain. Its pipeline (`domains/policy/rag/`) is the original code, copied
with only the import paths changed — the original package was called `app`,
which clashes with this project's `app.py`. Its settings live in
`config/policy.py` under `POLICY_*` env vars, so they can't collide with the
operations domain's `OPENAI_MODEL`.

**How a question is answered:** condense a follow-up into a standalone
question (only when there is history) → ChromaDB search (cosine) → drop
weak matches (`POLICY_RETRIEVAL_MAX_DISTANCE`) → `gpt-4o-mini` answers from
the retrieved chunks only, with the guardrails in
`domains/policy/rag/prompts.py`. When nothing relevant is found it replies
with a fixed "please contact support" message. Follow-up memory is per
session, keyed by the same `sid` as the other domains.

**Model:** always `gpt-4o-mini` (`POLICY_MODEL` in `config/policy.py`),
regardless of `OPENAI_MODEL` / `SHOPPING_LLM_MODEL`.

**ChromaDB setup:** the built index ships in `domains/policy/chroma/`
(113 chunks, built with `openai:text-embedding-3-small`), so nothing needs
building for a normal start — startup logs
`Policy domain ready: 113 chunks...`, and `GET /api/policy/health` reports
the index status. When the knowledge base changes, replace the document in
`domains/policy/knowledge/` (keep only one copy there; old versions can go
in `domains/policy/knowledge/archive/`, which is ignored) and rebuild:

```bash
python -m scripts.ingest_policy           # upsert changed chunks, drop removed ones
python -m scripts.ingest_policy --reset   # full rebuild (needed after changing the embedding model)
```

In Docker the index is baked into the image, so rebuild the image after
re-ingesting.

**Persistence:** every policy turn is saved like an operations turn — to
`conversation_messages` (by `app.py`, `domain='policy'`) and to
`chatbot_messages` (`intent='policy_query'`, `flow_name='policy'`). The
assistant row's `metadata` JSON holds the RAG details: `entry` (how the
message reached the policy domain: `free_text`, `mid_flow`, `policy_menu`,
`shopping_off_topic`, `operations_intent`), `model`, `grounded`,
`standalone_question` and the retrieved `sources` (section, subsection, FAQ
question, similarity score). Turns that reach policy through operations'
`general_policy` intent are saved by operations with `intent='general_policy'`
and the same metadata.

**Database changes:** run `migrations/add_policy_domain_columns.sql` on
existing databases **before** deploying this version — it adds
`conversation_messages.domain` (written for every turn) and
`conversation_sessions.ended_at` (written when a session closes).

**Environment variables** (all optional, defaults in brackets):

| Variable | Default | Notes |
|---|---|---|
| `POLICY_OPENAI_API_KEY` | `OPENAI_API_KEY` | Used for answers and query embeddings. |
| `POLICY_EMBEDDING_PROVIDER` | `openai` | `openai` or `local` (offline ONNX MiniLM). Re-ingest with `--reset` after changing. |
| `POLICY_EMBEDDING_MODEL` | `text-embedding-3-small` | Must match the model the index was built with. |
| `POLICY_DOCUMENTS_DIR` | `domains/policy/knowledge` | Source documents for ingest. |
| `POLICY_CHROMA_DIR` | `domains/policy/chroma` | ChromaDB persistent directory. |
| `POLICY_CHROMA_COLLECTION` | `naheed_policy` | |
| `POLICY_RETRIEVAL_TOP_K` | `5` | Chunks sent to the LLM. |
| `POLICY_RETRIEVAL_MAX_DISTANCE` | `0.75` | Lower = stricter relevance. |
| `POLICY_LLM_TEMPERATURE` / `POLICY_LLM_MAX_TOKENS` | `0.1` / `500` | |
| `POLICY_HISTORY_TURNS` / `POLICY_SESSION_TTL_MINUTES` | `4` / `60` | Follow-up memory. |
| `POLICY_MAX_INPUT_CHARS` | `1000` | |
| `POLICY_SUPPORT_PHONE` / `POLICY_SUPPORT_EMAIL` | `(021) 111-624-333` / `support@naheed.pk` | Used in fallback replies. |
| `POLICY_CHUNK_MAX_CHARS` / `_MIN_CHARS` / `_OVERLAP_CHARS` | `1200` / `150` / `200` | Ingest only. |

## Logging

All logging goes through `utils/logger.py`: one root setup (console +
`logs/application.log`), level from `LOG_LEVEL` (default `INFO`), with
HTTP/SDK libraries (httpx, openai, chromadb, urllib3, mysql) kept at
`WARNING`. At `INFO` you see startup/initialisation, the domain each message
was routed to, intent decisions and key state changes (order cancelled,
ticket created, session closed, LLM provider health). Per-request
diagnostics are at `DEBUG`.

## Known items from the source projects worth knowing about

- `npk_shopping_chatbot`'s `.gitignore` file currently contains what look
  like real credential values instead of ignore patterns — worth checking
  that project's git history for leaked secrets and rotating any keys that
  may have been committed. This new project's `.gitignore` is a normal one.
- The original shopping bot's chat UI referenced a cart sidebar in its CSS
  and JavaScript that was never actually present in the page's HTML (the
  cart button did nothing). This project's `templates/index.html` includes
  the missing markup so the cart sidebar actually works.
