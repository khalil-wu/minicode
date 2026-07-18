"""
记忆统一管理层（DESIGN.md §2 / §3 / §4）。

对上提供：
  - load_index()
  - read_file() / save_file()
  - append_facts()
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from backend.memory.file_memory import FileMemory

MemoryType = Literal["user", "feedback", "project", "reference"]

MEMORY_TYPES: tuple[MemoryType, ...] = ("user", "feedback", "project", "reference")

_AUTO_MEMORY_FILES: dict[MemoryType, tuple[str, str, str]] = {
    "user": (
        "auto_user.md",
        "Auto user memories",
        "Automatically extracted user profile, goals, and background",
    ),
    "feedback": (
        "auto_feedback.md",
        "Auto feedback memories",
        "Automatically extracted behavior feedback and collaboration preferences",
    ),
    "project": (
        "auto_project.md",
        "Auto project memories",
        "Automatically extracted project context not derivable from code",
    ),
    "reference": (
        "auto_reference.md",
        "Auto reference memories",
        "Automatically extracted pointers to external systems and docs",
    ),
}

_TYPE_PREFIX_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:(?:\[(user|feedback|project|reference)\])|"
    r"(?:(user|feedback|project|reference)\s*[:：|\-]))\s*(.+)$",
    re.IGNORECASE,
)
_INDEX_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)\s*[—-]\s*(.+)$")
_FILE_PATH_RE = re.compile(
    r"(^|[\s`'\"(])(?:[A-Za-z]:)?(?:[\w.-]+[\\/])+[\w .@()#-]+",
    re.IGNORECASE,
)
_FILE_EXTENSION_RE = re.compile(
    r"\b[\w.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|md|json|ya?ml|toml|css|scss|html|sql)\b",
    re.IGNORECASE,
)
_CAMEL_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Za-z0-9]+[A-Z][A-Za-z0-9]*\b")


@dataclass(frozen=True)
class MemoryFact:
    type: MemoryType
    text: str


@dataclass(frozen=True)
class MemoryHeader:
    filename: str
    description: str
    type: MemoryType | None
    age_days: float | None = None


class MemoryManager:
    """File-memory facade.

    Long-term agent memory is intentionally file-backed: MEMORY.md plus named
    markdown files. Vector storage remains available to document ingestion/RAG,
    but it is not part of the default memory manager.
    """

    def __init__(
        self,
        file_memory: FileMemory | None = None,
    ) -> None:
        self._file_memory = file_memory or FileMemory()

    def load_index(self) -> str:
        """加载 MEMORY.md 轻量索引。"""
        return self._file_memory.get_index()

    def list_memory_files(self) -> list[str]:
        return self._file_memory.list_files()

    def read_file(self, filename: str) -> str | None:
        return self._file_memory.read_file(filename)

    def save_file(self, filename: str, content: str, description: str | None = None) -> bool:
        ok = self._file_memory.save_file(filename, content)
        if ok and description:
            self._file_memory.update_index_entry(filename, description)
        return ok

    def append_facts(
        self,
        facts: list[str | MemoryFact | dict[str, Any]],
        *,
        filename: str | None = None,
    ) -> bool:
        """Append autocompact-extracted facts to the file track (CC-aligned).

        Semantic facts recovered during compaction belong in a durable,
        human-readable memory file — not a vector store keyed by similarity.
        Facts are appended with an ISO date so the staleness reminder in
        read_memory can flag them once they age. Deduplicates against lines
        already present so repeated compactions don't pile up duplicates.
        """
        if filename:
            return self._append_legacy_facts(
                [str(fact).strip() for fact in facts if str(fact or "").strip()],
                filename=filename,
            )

        typed_facts = [
            fact
            for fact in (self._coerce_fact(raw) for raw in facts)
            if fact is not None and self._should_keep_fact(fact)
        ]
        if not typed_facts:
            return False

        wrote = False
        for memory_type in MEMORY_TYPES:
            bucket = [fact.text for fact in typed_facts if fact.type == memory_type]
            if not bucket:
                continue
            target, title, description = _AUTO_MEMORY_FILES[memory_type]
            wrote = self._append_typed_facts(
                bucket,
                filename=target,
                title=title,
                description=description,
                memory_type=memory_type,
            ) or wrote
        return wrote

    def scan_memory_headers(self, *, max_files: int = 200) -> list[MemoryHeader]:
        """Return frontmatter/index summaries for model-based memory selection."""
        index_descriptions = self._index_descriptions()
        headers: list[MemoryHeader] = []
        for filename in self._file_memory.list_files():
            if filename == "MEMORY.md":
                continue
            content = self._file_memory.read_file(filename)
            if not content or not self._has_meaningful_body(content):
                continue
            frontmatter = self._parse_frontmatter(content)
            description = str(
                frontmatter.get("description")
                or index_descriptions.get(filename)
                or ""
            ).strip()
            memory_type = self._parse_memory_type(frontmatter.get("type"))
            headers.append(
                MemoryHeader(
                    filename=filename,
                    description=description,
                    type=memory_type,
                    age_days=self._file_memory.file_age_days(filename),
                )
            )
        return sorted(
            headers,
            key=lambda item: (
                float("inf") if item.age_days is None else item.age_days,
                item.filename,
            ),
        )[: max(0, int(max_files))]

    def _append_legacy_facts(self, facts: list[str], *, filename: str) -> bool:
        """Compatibility path for callers that explicitly request one file."""
        clean = [fact.strip() for fact in facts if fact and fact.strip()]
        if not clean:
            return False
        existing = self._file_memory.read_file(filename) or ""
        if not existing.strip():
            existing = "# 自动抽取的事实\n\n<!-- AutoCompact 从压缩对话中保留的高层结论 -->\n"
        # Dedup on the fact text alone (strip the leading "- " and any trailing
        # date comment) so a fact re-extracted on a later date isn't re-added.
        existing_facts = set()
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                existing_facts.add(stripped[2:].split(" <!--")[0].strip())
        stamp = time.strftime("%Y-%m-%d")
        new_lines = [
            f"- {fact} <!-- {stamp} -->"
            for fact in clean
            if fact not in existing_facts
        ]
        if not new_lines:
            return False
        body = existing.rstrip() + "\n" + "\n".join(new_lines) + "\n"
        ok = self._file_memory.save_file(filename, body)
        if ok:
            self._file_memory.update_index_entry(filename, "AutoCompact 保留的高层结论")
        return ok

    def _append_typed_facts(
        self,
        facts: list[str],
        *,
        filename: str,
        title: str,
        description: str,
        memory_type: MemoryType,
    ) -> bool:
        clean = [fact.strip() for fact in facts if fact and fact.strip()]
        if not clean:
            return False
        existing = self._file_memory.read_file(filename) or ""
        if not existing.strip():
            existing = (
                "---\n"
                f"name: {_path_like_stem(filename)}\n"
                f"description: {description}\n"
                f"type: {memory_type}\n"
                "---\n\n"
                f"# {title}\n\n"
            )
        existing_facts = self._existing_fact_lines(existing)
        stamp = time.strftime("%Y-%m-%d")
        new_lines = [
            f"- {fact} <!-- {stamp} -->"
            for fact in clean
            if fact not in existing_facts
        ]
        if not new_lines:
            return False
        body = existing.rstrip() + "\n" + "\n".join(new_lines) + "\n"
        ok = self._file_memory.save_file(filename, body)
        if ok:
            self._file_memory.update_index_entry(filename, description)
        return ok

    @staticmethod
    def _existing_fact_lines(content: str) -> set[str]:
        facts: set[str] = set()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                facts.add(stripped[2:].split(" <!--")[0].strip())
        return facts

    def _coerce_fact(self, raw: str | MemoryFact | dict[str, Any]) -> MemoryFact | None:
        if isinstance(raw, MemoryFact):
            text = raw.text.strip()
            return MemoryFact(raw.type, text) if text else None
        if isinstance(raw, dict):
            memory_type = self._parse_memory_type(raw.get("type"))
            text = str(raw.get("text") or raw.get("content") or raw.get("fact") or "").strip()
            if memory_type and text:
                return MemoryFact(memory_type, text)
            return None

        text = str(raw or "").strip()
        if not text:
            return None
        match = _TYPE_PREFIX_RE.match(text)
        if match:
            raw_type = match.group(1) or match.group(2)
            memory_type = self._parse_memory_type(raw_type)
            fact_text = match.group(3).strip()
            if memory_type and fact_text:
                return MemoryFact(memory_type, fact_text)
            return None
        return MemoryFact(self._infer_memory_type(text), text)

    @staticmethod
    def _parse_memory_type(raw: Any) -> MemoryType | None:
        value = str(raw or "").strip().lower()
        return value if value in MEMORY_TYPES else None  # type: ignore[return-value]

    @staticmethod
    def _infer_memory_type(text: str) -> MemoryType:
        lower = text.lower()
        if re.search(r"https?://|\b(grafana|linear|jira|slack|notion|dashboard|runbook|docs?)\b", lower):
            return "reference"
        has_user_marker = "用户" in lower or re.search(r"\buser\b", lower)
        has_profile_marker = (
            re.search(r"\b(role|background|responsibilit|goal|knows|expert)\b", lower)
            or any(marker in lower for marker in ("熟悉", "经验", "职责", "目标", "背景"))
        )
        if has_user_marker and has_profile_marker:
            return "user"
        if re.search(
            r"\b(prefer|prefers|wants|does not want|don't|do not|stop|avoid|keep|should|must)\b|偏好|不要|别再|纠正|反馈",
            lower,
        ):
            return "feedback"
        return "project"

    @staticmethod
    def _should_keep_fact(fact: MemoryFact) -> bool:
        text = fact.text.strip()
        if len(text) < 8:
            return False
        lower = text.lower()

        transient_markers = (
            "current task",
            "this task",
            "this session",
            "just fixed",
            "tests passed",
            "pending task",
            "next step",
            "todo",
            "当前任务",
            "本轮",
            "这次会话",
            "刚刚",
            "已完成",
            "测试通过",
            "待办",
            "下一步",
        )
        if any(marker in lower for marker in transient_markers):
            return False

        git_markers = (
            "git log",
            "git blame",
            "commit ",
            "branch ",
            "pr #",
            "pull request",
            "recent change",
            "最近修改",
            "提交",
            "分支",
        )
        if any(marker in lower for marker in git_markers):
            return False

        if fact.type != "reference" and (
            _FILE_PATH_RE.search(text) or _FILE_EXTENSION_RE.search(text)
        ):
            return False

        if fact.type == "project" and re.search(
            r"\b(uses|contains|implements|calls|class|function|module|component|service|tool)\b",
            lower,
        ) and _CAMEL_IDENTIFIER_RE.search(text):
            return False

        return True

    def _index_descriptions(self) -> dict[str, str]:
        descriptions: dict[str, str] = {}
        try:
            index = self._file_memory.get_index()
        except Exception:
            return descriptions
        for line in index.splitlines():
            match = _INDEX_LINK_RE.search(line.strip())
            if match:
                descriptions[match.group(1)] = match.group(2).strip()
        return descriptions

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, str]:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        parsed: dict[str, str] = {}
        for line in lines[1:31]:
            if line.strip() == "---":
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            parsed[key.strip()] = value.strip().strip("\"'")
        return parsed

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return content
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[index + 1 :])
        return content

    @classmethod
    def _has_meaningful_body(cls, content: str) -> bool:
        body = cls._strip_frontmatter(content)
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("<!--") and stripped.endswith("-->"):
                continue
            if stripped.startswith("#"):
                continue
            return True
        return False


def _path_like_stem(filename: str) -> str:
    return filename.rsplit(".", 1)[0].replace(" ", "_")
