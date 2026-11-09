#!/usr/bin/env python3
"""
knowledge_lookup(query) — semantic search across 4 ChromaDB collections.
Returns a formatted string ready to inject into an agent prompt.
"""

from pathlib import Path
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

_CHROMA_DIR = Path(__file__).parent / "chromadb"
_client = None
_ef = None
_collections: dict = {}


def _init():
    global _client, _ef, _collections
    if _client is not None:
        return
    _ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    _client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
    for name in ["deal_notes", "kb_documents", "company_profiles", "meddpicc_scores"]:
        try:
            _collections[name] = _client.get_collection(name, embedding_function=_ef)
        except Exception:
            pass


def knowledge_lookup(
    query: str,
    collections: list[str] | None = None,
    n_results: int = 3,
    metadata_filters: dict | None = None,
) -> str:
    """
    Search ChromaDB collections and return formatted results for prompt injection.

    Args:
        query:            Natural language search query
        collections:      List of collection names to search (default: all 4)
        n_results:        Results per collection
        metadata_filters: Optional ChromaDB where clause (applied to all collections)

    Returns:
        Formatted string with SOURCE headers for each result chunk
    """
    _init()

    if collections is None:
        collections = ["deal_notes", "kb_documents", "company_profiles", "meddpicc_scores"]

    source_labels = {
        "deal_notes":       lambda m: f"Deal Note — {m.get('company_name', '')} / {m.get('deal_name', '')} / {m.get('note_type', '')}",
        "kb_documents":     lambda m: f"KB {m.get('document_type', '').replace('_', ' ').title()} — {m.get('base_category', '')}",
        "company_profiles": lambda m: f"Company Profile — {m.get('company_name', '')}",
        "meddpicc_scores":  lambda m: f"MEDDPICC — {m.get('company_name', '')} / {m.get('deal_name', '')}",
    }

    seen_ids: set[str] = set()
    result_parts: list[str] = []

    for col_name in collections:
        col = _collections.get(col_name)
        if col is None:
            continue
        try:
            kwargs = {"query_texts": [query], "n_results": min(n_results, col.count())}
            if metadata_filters:
                kwargs["where"] = metadata_filters
            results = col.query(**kwargs)

            ids = results.get("ids", [[]])[0]
            docs = results.get("documents", [[]])[0]
            meta_list = results.get("metadatas", [[]])[0]

            for chunk_id, doc, meta in zip(ids, docs, meta_list):
                if chunk_id in seen_ids:
                    continue
                seen_ids.add(chunk_id)
                label = source_labels.get(col_name, lambda m: col_name)(meta)
                result_parts.append(f"[SOURCE: {label}]\n{doc}")
        except Exception as e:
            result_parts.append(f"[SOURCE: {col_name} — error: {e}]")

    return "\n\n".join(result_parts) if result_parts else "[No relevant knowledge found]"


# ── SELF-TEST ──────────────────────────────────────────────────────────────

