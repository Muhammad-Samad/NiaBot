"""Document loading, cleaning and chunking."""
from __future__ import annotations

from pathlib import Path

from config.policy import settings
from domains.policy.rag.ingestion.chunker import Chunk, chunk_document
from domains.policy.rag.ingestion.cleaner import clean_text
from domains.policy.rag.ingestion.loaders import load_directory


def build_chunks(documents_dir: Path | None = None) -> list[Chunk]:
    """Load every document in the folder and return its chunks."""
    chunks: list[Chunk] = []
    for doc in load_directory(documents_dir or settings.documents_dir):
        chunks.extend(
            chunk_document(
                clean_text(doc.text),
                source=doc.source,
                max_chars=settings.chunk_max_chars,
                overlap_chars=settings.chunk_overlap_chars,
                min_chars=settings.chunk_min_chars,
            )
        )
    return chunks


__all__ = ["Chunk", "build_chunks", "chunk_document", "clean_text"]
