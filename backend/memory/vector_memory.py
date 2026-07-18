"""
Vector memory with ChromaDB primary storage and a lightweight JSON fallback.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.chroma_utils import create_chroma_persistent_client
from backend.config import DATA_ROOT

logger = logging.getLogger(__name__)

CHROMA_DIR = DATA_ROOT / "chroma"
FALLBACK_DB_FILE = CHROMA_DIR / "memory_fallback.json"
FALLBACK_FLUSH_INTERVAL_SECONDS = 5.0
FALLBACK_FLUSH_BATCH_SIZE = 10
CHROMA_HNSW_METADATA = {
    "hnsw:space": "cosine",
    "hnsw:ef_construction": 200,
    "hnsw:M": 32,
    "hnsw:search_ef": 100,
}


@dataclass
class MemoryEntry:
    memory_id: str
    content: str
    tags: list[str]
    importance: int
    metadata: dict[str, Any]


class VectorMemory:
    """Semantic memory backed by ChromaDB with an indexed JSON fallback."""
    _shared_clients: dict[str, Any] = {}

    def __init__(self, storage_dir: Path | None = None, collection_name: str = "memory") -> None:
        self._storage_dir = storage_dir or CHROMA_DIR
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._collection_name = collection_name
        self._fallback_file = self._storage_dir / f"{collection_name}_fallback.json"
        self._client = None
        self._collection = None
        self._fallback_entries: dict[str, MemoryEntry] = {}
        self._fallback_entry_tokens: dict[str, set[str]] = {}
        self._fallback_token_index: dict[str, set[str]] = {}
        self._dirty = False
        self._dirty_since: float | None = None
        self._pending_mutations = 0
        self._init_backend()

    def _init_backend(self) -> None:
        try:
            import chromadb

            storage_key = str(self._storage_dir.resolve())
            self._client = self._shared_clients.get(storage_key)
            if self._client is None:
                self._client = create_chroma_persistent_client(chromadb, self._storage_dir)
                self._shared_clients[storage_key] = self._client
            self._collection = self._get_or_create_collection(self._collection_name)
            logger.info("Vector memory using ChromaDB collection=%s", self._collection_name)
        except Exception as exc:
            logger.info(
                "ChromaDB unavailable, falling back to JSON similarity search: %s",
                exc,
            )
            self._load_fallback_entries()

    def _get_or_create_collection(self, name: str) -> Any:
        try:
            return self._client.get_or_create_collection(
                name,
                metadata=CHROMA_HNSW_METADATA,
            )
        except TypeError:
            return self._client.get_or_create_collection(name)

    def _load_fallback_entries(self) -> None:
        if not self._fallback_file.exists():
            self._fallback_entries = {}
            self._rebuild_fallback_index()
            return

        try:
            data = json.loads(self._fallback_file.read_text(encoding="utf-8"))
            self._fallback_entries = {
                item["memory_id"]: MemoryEntry(**item)
                for item in data
                if isinstance(item, dict) and item.get("memory_id")
            }
        except Exception as exc:
            logger.warning("Failed to load fallback vector memory: %s", exc)
            self._fallback_entries = {}

        self._dirty = False
        self._dirty_since = None
        self._pending_mutations = 0
        self._rebuild_fallback_index()

    def _save_fallback_entries(self) -> None:
        try:
            payload = [asdict(entry) for entry in self._fallback_entries.values()]
            self._fallback_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._dirty = False
        except Exception as exc:
            logger.error("Failed to save fallback vector memory: %s", exc)

    def flush(self) -> None:
        if not self._dirty:
            return
        self._commit_fallback_flush()

    def __del__(self) -> None:
        try:
            self.flush()
        except Exception:
            pass

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        importance: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        memory_id = f"mem_{uuid.uuid4().hex[:10]}"
        tags = tags or []
        metadata = metadata or {}
        importance = max(1, min(5, importance))

        if self._collection is not None:
            try:
                self._collection.add(
                    ids=[memory_id],
                    documents=[content],
                    metadatas=[
                        {
                            "tags": json.dumps(tags, ensure_ascii=False),
                            "importance": importance,
                            **metadata,
                        }
                    ],
                )
                return memory_id
            except Exception as exc:
                logger.warning("Chroma write failed, falling back to JSON store: %s", exc)

        entry = MemoryEntry(
            memory_id=memory_id,
            content=content,
            tags=tags,
            importance=importance,
            metadata=metadata,
        )
        self._fallback_entries[memory_id] = entry
        self._index_fallback_entry(entry)
        self._mark_fallback_dirty()
        return memory_id

    def recall(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.7,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []

        if self._collection is not None:
            try:
                result = self._collection.query(query_texts=[query], n_results=max(1, top_k))
                ids = (result.get("ids") or [[]])[0]
                docs = (result.get("documents") or [[]])[0]
                metas = (result.get("metadatas") or [[]])[0]
                distances = (result.get("distances") or [[]])[0]

                items: list[dict[str, Any]] = []
                for idx, memory_id in enumerate(ids):
                    doc = docs[idx] if idx < len(docs) else ""
                    meta = metas[idx] if idx < len(metas) else {}
                    distance = distances[idx] if idx < len(distances) else 0.0
                    score = self._distance_to_score(distance)
                    if score < min_score:
                        continue
                    raw_tags = meta.get("tags", "[]")
                    tags = self._safe_json_loads(raw_tags, default=[])
                    items.append(
                        {
                            "memory_id": memory_id,
                            "summary": self._make_summary(doc),
                            "score": round(score, 4),
                            "tags": tags,
                            "importance": int(meta.get("importance", 3)),
                        }
                    )
                return items
            except Exception as exc:
                logger.warning("Chroma query failed, falling back to JSON store: %s", exc)

        query_tokens = self._tokenize(query)
        candidate_ids = self._candidate_fallback_ids(query_tokens, min_score=min_score)

        scored: list[tuple[float, MemoryEntry]] = []
        for memory_id in candidate_ids:
            entry = self._fallback_entries.get(memory_id)
            tokens = self._fallback_entry_tokens.get(memory_id)
            if entry is None or tokens is None:
                continue
            score = self._fallback_similarity(query_tokens, tokens)
            if score >= min_score:
                scored.append((score, entry))

        scored.sort(key=lambda item: (item[0], item[1].importance), reverse=True)
        return [
            {
                "memory_id": entry.memory_id,
                "summary": self._make_summary(entry.content),
                "score": round(score, 4),
                "tags": entry.tags,
                "importance": entry.importance,
            }
            for score, entry in scored[:top_k]
        ]

    def get_memory(self, memory_id: str) -> str | None:
        if self._collection is not None:
            try:
                result = self._collection.get(ids=[memory_id])
                docs = result.get("documents") or []
                if docs:
                    return docs[0]
            except Exception as exc:
                logger.warning("Chroma get_memory failed: %s", exc)

        entry = self._fallback_entries.get(memory_id)
        return entry.content if entry else None

    def forget(self, memory_id: str) -> bool:
        removed = False
        if self._collection is not None:
            try:
                self._collection.delete(ids=[memory_id])
                removed = True
            except Exception as exc:
                logger.warning("Chroma forget failed: %s", exc)

        if memory_id in self._fallback_entries:
            self._fallback_entries.pop(memory_id, None)
            self._remove_fallback_entry_index(memory_id)
            self._mark_fallback_dirty()
            removed = True
        return removed

    def list_memories(self, tag: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        if self._collection is not None:
            try:
                result = self._collection.get(limit=limit)
                ids = result.get("ids") or []
                docs = result.get("documents") or []
                metas = result.get("metadatas") or []
                for idx, memory_id in enumerate(ids):
                    meta = metas[idx] if idx < len(metas) else {}
                    raw_tags = meta.get("tags", "[]")
                    tags = self._safe_json_loads(raw_tags, default=[])
                    if tag and tag not in tags:
                        continue
                    doc = docs[idx] if idx < len(docs) else ""
                    items.append(
                        {
                            "memory_id": memory_id,
                            "summary": self._make_summary(doc),
                            "tags": tags,
                            "importance": int(meta.get("importance", 3)),
                        }
                    )
                    if len(items) >= limit:
                        break
                return items
            except Exception as exc:
                logger.warning("Chroma list_memories failed, using JSON fallback: %s", exc)

        for entry in self._fallback_entries.values():
            if tag and tag not in entry.tags:
                continue
            items.append(
                {
                    "memory_id": entry.memory_id,
                    "summary": self._make_summary(entry.content),
                    "tags": entry.tags,
                    "importance": entry.importance,
                }
            )
        items.sort(key=lambda item: item["importance"], reverse=True)
        return items[:limit]

    def _rebuild_fallback_index(self) -> None:
        self._fallback_entry_tokens = {}
        self._fallback_token_index = {}
        for entry in self._fallback_entries.values():
            self._index_fallback_entry(entry)

    def _index_fallback_entry(self, entry: MemoryEntry) -> None:
        self._remove_fallback_entry_index(entry.memory_id)
        tokens = self._tokenize(entry.content)
        self._fallback_entry_tokens[entry.memory_id] = tokens
        for token in tokens:
            self._fallback_token_index.setdefault(token, set()).add(entry.memory_id)

    def _remove_fallback_entry_index(self, memory_id: str) -> None:
        tokens = self._fallback_entry_tokens.pop(memory_id, None)
        if not tokens:
            return
        for token in tokens:
            bucket = self._fallback_token_index.get(token)
            if bucket is None:
                continue
            bucket.discard(memory_id)
            if not bucket:
                self._fallback_token_index.pop(token, None)

    def _candidate_fallback_ids(self, query_tokens: set[str], *, min_score: float) -> set[str]:
        if min_score <= 0.0:
            return set(self._fallback_entries.keys())

        candidate_ids: set[str] = set()
        for token in query_tokens:
            candidate_ids.update(self._fallback_token_index.get(token, set()))
        return candidate_ids

    def _mark_fallback_dirty(self) -> None:
        self._dirty = True
        if self._dirty_since is None:
            self._dirty_since = time.monotonic()
        self._pending_mutations += 1
        self._maybe_flush_fallback()

    def _maybe_flush_fallback(self) -> None:
        if not self._dirty:
            return

        if self._pending_mutations >= FALLBACK_FLUSH_BATCH_SIZE:
            self._commit_fallback_flush()
            return

        if (
            self._dirty_since is not None
            and (time.monotonic() - self._dirty_since) >= FALLBACK_FLUSH_INTERVAL_SECONDS
        ):
            self._commit_fallback_flush()

    def _commit_fallback_flush(self) -> None:
        if not self._dirty:
            return
        self._save_fallback_entries()
        if not self._dirty:
            self._pending_mutations = 0
            self._dirty_since = None

    @staticmethod
    def _make_summary(content: str, max_chars: int = 100) -> str:
        text = re.sub(r"\s+", " ", content).strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    @staticmethod
    def _safe_json_loads(raw: Any, default: Any) -> Any:
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, str):
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    @staticmethod
    def _distance_to_score(distance: float) -> float:
        if distance is None:
            return 0.0
        return max(0.0, min(1.0, 1.0 / (1.0 + float(distance))))

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[\w\u4e00-\u9fff]+", text.lower()))

    @classmethod
    def _fallback_similarity(cls, a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        if inter == 0:
            return 0.0
        return inter / math.sqrt(len(a) * len(b))
