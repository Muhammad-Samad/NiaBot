"""
Retrieval: vector search -> relevance threshold -> de-duplication.

We over-fetch (2 x top_k) and then drop weak matches (cosine distance above
RETRIEVAL_MAX_DISTANCE) so the LLM only sees chunks that are actually about
the question. An empty result is a meaningful signal: it lets the bot fall
back to "please contact support" instead of guessing.
"""
from __future__ import annotations

import logging

from config.policy import settings
from domains.policy.rag.vectorstore import PolicyVectorStore, SearchResult

logger = logging.getLogger(__name__)


class PolicyRetriever:
    def __init__(self, store: PolicyVectorStore | None = None):
        self.store = store or PolicyVectorStore()

    def retrieve(self, query: str, k: int | None = None, max_distance: float | None = None) -> list[SearchResult]:
        k = k or settings.top_k
        max_distance = settings.max_distance if max_distance is None else max_distance

        candidates = self.store.search(query, k=k * 2)
        relevant = [r for r in candidates if r.distance <= max_distance]

        # The same fact often appears in a policy section, the FAQ and the
        # key-facts list. Keep the best-scoring copy of identical bodies.
        seen: set[str] = set()
        unique: list[SearchResult] = []
        for r in relevant:
            body = r.text.split("\n", 1)[-1].strip().lower()
            if body in seen:
                continue
            seen.add(body)
            unique.append(r)

        results = unique[:k]
        logger.debug(
            "retrieve(%r): %d candidates, %d relevant -> %s",
            query, len(candidates), len(results),
            [(r.metadata.get("section_title"), round(r.distance, 3)) for r in results],
        )
        return results
