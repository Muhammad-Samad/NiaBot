"""
Embedding providers. We compute vectors ourselves and hand them to Chroma,
which keeps the provider swappable and makes the model used at ingest time
explicit (it is stored on the collection and checked at query time).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from functools import lru_cache

from config.policy import settings

logger = logging.getLogger(__name__)


class Embedder(ABC):
    name: str

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class OpenAIEmbedder(Embedder):
    BATCH_SIZE = 100

    def __init__(self, model: str, api_key: str):
        if not api_key:
            raise RuntimeError("POLICY_OPENAI_API_KEY (or OPENAI_API_KEY) is not set (required for POLICY_EMBEDDING_PROVIDER=openai)")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self.name = f"openai:{model}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH_SIZE):
            batch = texts[start:start + self.BATCH_SIZE]
            resp = self._client.embeddings.create(model=self._model, input=batch)
            vectors.extend(item.embedding for item in sorted(resp.data, key=lambda d: d.index))
        return vectors


class LocalEmbedder(Embedder):
    """Offline embeddings using Chroma's built-in all-MiniLM-L6-v2 (ONNX).
    No API key and no PyTorch needed; the ~80 MB model downloads on first use."""

    def __init__(self):
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._ef = DefaultEmbeddingFunction()
        self.name = "local:all-MiniLM-L6-v2-onnx"

    BATCH_SIZE = 8  # small batches keep ONNX memory use low on modest machines

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH_SIZE):
            vectors.extend(list(map(float, v)) for v in self._ef(texts[start:start + self.BATCH_SIZE]))
        return vectors


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder(settings.openai_embedding_model, settings.openai_api_key)
    if settings.embedding_provider == "local":
        return LocalEmbedder()
    raise ValueError(f"Unknown POLICY_EMBEDDING_PROVIDER '{settings.embedding_provider}' (use 'openai' or 'local')")
