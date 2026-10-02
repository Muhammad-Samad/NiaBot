"""
ChromaDB wrapper: persistent client, cosine-distance collection, upsert and
similarity search.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import chromadb

from config.policy import settings
from domains.policy.rag.ingestion.chunker import Chunk
from domains.policy.rag.vectorstore.embeddings import Embedder, get_embedder

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    id: str
    text: str
    metadata: dict
    distance: float

    @property
    def score(self) -> float:
        """Cosine similarity (1 = identical)."""
        return 1.0 - self.distance


class EmbeddingMismatchError(RuntimeError):
    pass


class PolicyVectorStore:
    def __init__(self, embedder: Embedder | None = None):
        settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self._embedder = embedder or get_embedder()
        self._collection = None

    # ─── collection management ───────────────────────────────────────────
    @property
    def collection(self):
        if self._collection is None:
            self._collection = self._client.get_or_create_collection(
                name=settings.collection_name,
                configuration={"hnsw": {"space": "cosine"}},
                metadata={"embedding_model": self._embedder.name},
                embedding_function=None,  # we always pass vectors explicitly
            )
            stored = (self._collection.metadata or {}).get("embedding_model")
            if stored and stored != self._embedder.name:
                raise EmbeddingMismatchError(
                    f"Collection '{settings.collection_name}' was built with '{stored}' but the current "
                    f"embedding model is '{self._embedder.name}'. Re-run: python -m scripts.ingest_policy --reset"
                )
        return self._collection

    def reset(self) -> None:
        try:
            self._client.delete_collection(settings.collection_name)
            logger.info("Deleted collection %s", settings.collection_name)
        except Exception:  # collection did not exist
            pass
        self._collection = None

    def count(self) -> int:
        return self.collection.count()

    # ─── write ───────────────────────────────────────────────────────────
    def upsert(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        vectors = self._embedder.embed_documents([c.text for c in chunks])
        self.collection.upsert(
            ids=[c.id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=vectors,
            metadatas=[{**c.metadata, "ingested_at": ingested_at} for c in chunks],
        )
        return len(chunks)

    def delete_missing(self, keep_ids: set[str]) -> int:
        """Remove chunks that no longer exist in the source documents."""
        existing = set(self.collection.get(include=[])["ids"])
        stale = list(existing - keep_ids)
        if stale:
            self.collection.delete(ids=stale)
        return len(stale)

    # ─── read ────────────────────────────────────────────────────────────
    def search(self, query: str, k: int, where: dict | None = None) -> list[SearchResult]:
        if self.count() == 0:
            return []
        res = self.collection.query(
            query_embeddings=[self._embedder.embed_query(query)],
            n_results=min(k, self.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return [
            SearchResult(id=i, text=d, metadata=m or {}, distance=float(dist))
            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]
