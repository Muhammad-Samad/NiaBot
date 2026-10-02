"""
Build / refresh the policy domain's ChromaDB index from domains/policy/knowledge/.

    python -m scripts.ingest_policy           # incremental: upsert chunks, drop removed ones
    python -m scripts.ingest_policy --reset   # wipe the collection and rebuild from scratch

Re-run this whenever the policy document changes. Use --reset after changing
POLICY_EMBEDDING_PROVIDER / POLICY_EMBEDDING_MODEL.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from config.policy import settings
from domains.policy.rag.ingestion import build_chunks
from utils.logger import setup_logging
from domains.policy.rag.vectorstore import PolicyVectorStore

logger = logging.getLogger("ingest")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the collection before ingesting")
    args = parser.parse_args()
    setup_logging()

    started = time.time()
    logger.info("Reading documents from %s", settings.documents_dir)
    chunks = build_chunks()
    logger.info("Built %d chunks", len(chunks))

    store = PolicyVectorStore()
    if args.reset:
        store.reset()

    upserted = store.upsert(chunks)
    removed = store.delete_missing({c.id for c in chunks})
    logger.info(
        "Done in %.1fs: %d chunks upserted, %d stale removed, %d total in '%s' (%s)",
        time.time() - started, upserted, removed, store.count(), settings.collection_name, settings.chroma_dir,
    )


if __name__ == "__main__":
    main()
