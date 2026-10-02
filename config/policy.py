"""policy.py - Settings for the policy domain (ChromaDB-backed RAG).

Ported from General_Policy RAG's app/config.py: the same Settings dataclass
and field names, so the RAG modules under domains/policy/rag/ use it
unchanged. The differences are:
  - every env var is prefixed with POLICY_, so it can't collide with NiaBot's
    own OPENAI_MODEL / FLASK_* / LOG_LEVEL variables;
  - the answer model is fixed to gpt-4o-mini and can't be overridden;
  - relative paths resolve against the NiaBot project root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# The policy domain always answers with this model, whatever the other
# domains are configured to use.
POLICY_MODEL = "gpt-4o-mini"


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else BASE_DIR / p


@dataclass(frozen=True)
class Settings:
    # ─── Paths ────────────────────────────────────────────────────────────
    documents_dir: Path = field(default_factory=lambda: _path("POLICY_DOCUMENTS_DIR", "domains/policy/knowledge"))
    chroma_dir: Path = field(default_factory=lambda: _path("POLICY_CHROMA_DIR", "domains/policy/chroma"))
    collection_name: str = field(default_factory=lambda: os.getenv("POLICY_CHROMA_COLLECTION", "naheed_policy"))

    # ─── OpenAI ───────────────────────────────────────────────────────────
    # Falls back to the operations domain's OpenAI key when no dedicated
    # policy key is set.
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("POLICY_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    )
    openai_model: str = POLICY_MODEL
    llm_temperature: float = field(default_factory=lambda: _float("POLICY_LLM_TEMPERATURE", 0.1))
    llm_max_tokens: int = field(default_factory=lambda: _int("POLICY_LLM_MAX_TOKENS", 500))

    # ─── Embeddings ───────────────────────────────────────────────────────
    # "openai" -> OpenAI embeddings API, "local" -> Chroma's built-in ONNX MiniLM (offline)
    embedding_provider: str = field(default_factory=lambda: os.getenv("POLICY_EMBEDDING_PROVIDER", "openai").lower())
    openai_embedding_model: str = field(
        default_factory=lambda: os.getenv("POLICY_EMBEDDING_MODEL", "text-embedding-3-small")
    )

    # ─── Chunking ─────────────────────────────────────────────────────────
    chunk_max_chars: int = field(default_factory=lambda: _int("POLICY_CHUNK_MAX_CHARS", 1200))
    chunk_min_chars: int = field(default_factory=lambda: _int("POLICY_CHUNK_MIN_CHARS", 150))
    chunk_overlap_chars: int = field(default_factory=lambda: _int("POLICY_CHUNK_OVERLAP_CHARS", 200))

    # ─── Retrieval ────────────────────────────────────────────────────────
    top_k: int = field(default_factory=lambda: _int("POLICY_RETRIEVAL_TOP_K", 5))
    # Cosine distance (0 = identical, 2 = opposite). Chunks above this are dropped.
    max_distance: float = field(default_factory=lambda: _float("POLICY_RETRIEVAL_MAX_DISTANCE", 0.75))

    # ─── Conversation ─────────────────────────────────────────────────────
    history_turns: int = field(default_factory=lambda: _int("POLICY_HISTORY_TURNS", 4))
    max_input_chars: int = field(default_factory=lambda: _int("POLICY_MAX_INPUT_CHARS", 1000))
    session_ttl_minutes: int = field(default_factory=lambda: _int("POLICY_SESSION_TTL_MINUTES", 60))

    # ─── Support / fallback ───────────────────────────────────────────────
    support_phone: str = field(default_factory=lambda: os.getenv("POLICY_SUPPORT_PHONE", "(021) 111-624-333"))
    support_email: str = field(default_factory=lambda: os.getenv("POLICY_SUPPORT_EMAIL", "support@naheed.pk"))

    @property
    def fallback_message(self) -> str:
        return (
            "I couldn't find confirmed information about that in Naheed's available information. "
            f"Please contact Naheed customer support at {self.support_phone} or {self.support_email}."
        )


settings = Settings()
