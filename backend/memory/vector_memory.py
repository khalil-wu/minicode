"""
向量记忆（DESIGN.md §2.2-B / §4.3）。

优先使用 ChromaDB 做本地语义检索；若环境未安装 chromadb，
则退化为基于词项重叠的轻量检索，保证系统仍可运行。
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

CHROMA_DIR = PROJECT_ROOT / "data" / "chroma"
FALLBACK_DB_FILE = CHROMA_DIR / "memory_fallback.json"


@dataclass
class MemoryEntry:
    """单条向量记忆。"""

    memory_id: str
    content: str
    tags: list[str]
    importance: int
    metadata: dict[str, Any]


class VectorMemory:
    """
    向量记忆封装。

    接口对齐 DESIGN.md：
      - remember(content, tags, importance)
      - recall(query, top_k, min_score)
      - get_memory(memory_id)
      - forget(memory_id)
      - list_memories(tag, limit)
    """

    def __init__(self, storage_dir: Path | None = None, collection_name: str = "memory") -> None:
        self._storage_dir = storage_dir or CHROMA_DIR
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._collection_name = collection_name
        self._fallback_file = self._storage_dir / f"{collection_name}_fallback.json"
        self._client = None
        self._collection = None
        self._fallback_entries: dict[str, MemoryEntry] = {}
        self._init_backend()

    def _init_backend(self) -> None:
        """初始化 ChromaDB；失败时退化到 JSON fallback。"""
        try:
            import chromadb

            self._client = chromadb.PersistentClient(path=str(self._storage_dir))
            self._collection = self._client.get_or_create_collection(self._collection_name)
            logger.info("向量记忆使用 ChromaDB collection=%s", self._collection_name)
        except Exception as exc:
            logger.warning("ChromaDB 不可用，退化为 fallback 检索: %s", exc)
            self._load_fallback_entries()

    def _load_fallback_entries(self) -> None:
        if not self._fallback_file.exists():
            self._fallback_entries = {}
            return
        try:
            data = json.loads(self._fallback_file.read_text(encoding="utf-8"))
            self._fallback_entries = {
                item["memory_id"]: MemoryEntry(**item)
                for item in data
                if isinstance(item, dict) and item.get("memory_id")
            }
        except Exception as exc:
            logger.warning("加载 fallback 向量记忆失败: %s", exc)
            self._fallback_entries = {}

    def _save_fallback_entries(self) -> None:
        try:
            payload = [asdict(entry) for entry in self._fallback_entries.values()]
            self._fallback_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("保存 fallback 向量记忆失败: %s", exc)

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        importance: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """写入一条长期记忆。"""
        memory_id = f"mem_{uuid.uuid4().hex[:10]}"
        tags = tags or []
        metadata = metadata or {}
        importance = max(1, min(5, importance))

        if self._collection is not None:
            try:
                self._collection.add(
                    ids=[memory_id],
                    documents=[content],
                    metadatas=[{
                        "tags": json.dumps(tags, ensure_ascii=False),
                        "importance": importance,
                        **metadata,
                    }],
                )
                return memory_id
            except Exception as exc:
                logger.warning("Chroma 写入失败，退化到 fallback: %s", exc)

        self._fallback_entries[memory_id] = MemoryEntry(
            memory_id=memory_id,
            content=content,
            tags=tags,
            importance=importance,
            metadata=metadata,
        )
        self._save_fallback_entries()
        return memory_id

    def recall(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.7,
    ) -> list[dict[str, Any]]:
        """语义检索记忆，返回摘要列表。"""
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
                logger.warning("Chroma 查询失败，退化到 fallback: %s", exc)

        scored: list[tuple[float, MemoryEntry]] = []
        query_tokens = self._tokenize(query)
        for entry in self._fallback_entries.values():
            score = self._fallback_similarity(query_tokens, self._tokenize(entry.content))
            if score >= min_score:
                scored.append((score, entry))

        scored.sort(key=lambda item: (item[0], item[1].importance), reverse=True)
        results: list[dict[str, Any]] = []
        for score, entry in scored[:top_k]:
            results.append(
                {
                    "memory_id": entry.memory_id,
                    "summary": self._make_summary(entry.content),
                    "score": round(score, 4),
                    "tags": entry.tags,
                    "importance": entry.importance,
                }
            )
        return results

    def get_memory(self, memory_id: str) -> str | None:
        """读取完整记忆内容。"""
        if self._collection is not None:
            try:
                result = self._collection.get(ids=[memory_id])
                docs = result.get("documents") or []
                if docs:
                    return docs[0]
            except Exception as exc:
                logger.warning("Chroma get_memory 失败: %s", exc)

        entry = self._fallback_entries.get(memory_id)
        return entry.content if entry else None

    def forget(self, memory_id: str) -> bool:
        """删除一条记忆。"""
        removed = False
        if self._collection is not None:
            try:
                self._collection.delete(ids=[memory_id])
                removed = True
            except Exception as exc:
                logger.warning("Chroma forget 失败: %s", exc)

        if memory_id in self._fallback_entries:
            self._fallback_entries.pop(memory_id, None)
            self._save_fallback_entries()
            removed = True
        return removed

    def list_memories(self, tag: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """列出记忆元数据。"""
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
                logger.warning("Chroma list_memories 失败，退化到 fallback: %s", exc)

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
