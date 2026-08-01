"""
文件记忆（DESIGN.md §2.2-A）。

结构化、透明可审计的持久记忆：
  data/memory/
  ├── MEMORY.md              # 索引文件（启动时加载，每条 ≤ 80 chars）
  ├── user_profile.md        # 用户偏好、技术栈、工作风格
  ├── project_context.md     # 项目背景、当前目标、已决策事项
  ├── feedback.md            # 用户对 Agent 行为的纠正记录
  └── reference.md           # 外部资源、文档链接

MEMORY.md 格式（启动注入的是这个索引，不是正文）：
  - [user_profile](user_profile.md) — 全栈工程师，TypeScript 优先
  - [project_context](project_context.md) — MiniCode 学习项目，当前 Phase 2
"""

from __future__ import annotations

import logging
import hashlib
import os
import time
from pathlib import Path

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

MEMORY_DIR = DATA_ROOT / "memory"
MEMORY_INDEX_FILE = MEMORY_DIR / "MEMORY.md"

# Memories older than this are surfaced with a staleness warning so the model
# verifies before trusting them (Claude Code memory doc §11: memory is a
# historical snapshot, not present truth — code/paths/timelines must be
# re-checked against the live workspace before use).
STALE_THRESHOLD_DAYS = 14
MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_BYTES = 20_000

# 默认记忆文件模板
DEFAULT_MEMORY_INDEX = """\
- [user_profile](user_profile.md) — 用户偏好与工作风格
- [project_context](project_context.md) — 项目背景与当前目标
- [feedback](feedback.md) — 用户对 Agent 行为的纠正记录
- [reference](reference.md) — 外部资源与文档链接
"""

DEFAULT_MEMORY_FILES: dict[str, str] = {
    "user_profile.md": "# 用户偏好\n\n<!-- 记录用户的技术栈、工作风格、偏好设置 -->\n",
    "project_context.md": "# 项目背景\n\n<!-- 记录项目目标、当前阶段、已决策事项 -->\n",
    "feedback.md": "# 反馈记录\n\n<!-- 记录用户对 Agent 行为的纠正和反馈 -->\n",
    "reference.md": "# 外部资源\n\n<!-- 记录外部文档链接、参考资料 -->\n",
}


class FileMemory:
    """
    文件记忆管理器。

    职责：
    1. 管理 MEMORY.md 索引（启动时加载，只注入索引行）
    2. 读写具体记忆文件（Agent 通过 read_memory / save_memory 工具按需操作）
    """

    def __init__(self, memory_dir: Path | None = None) -> None:
        self._dir = memory_dir or MEMORY_DIR
        self._index_file = self._dir / "MEMORY.md"
        self._ensure_initialized()

    @classmethod
    def for_workspace(cls, workspace_root: Path | str | None) -> "FileMemory":
        """Return the project-scoped memory store for a workspace.

        Claude Code keys auto-memory by the canonical project rather than by
        the desktop process.  Resolve the nearest Git root and use a stable,
        opaque directory key so two projects never share project memory.
        """
        if not workspace_root:
            return cls()
        root = Path(workspace_root).expanduser().resolve()
        canonical = root
        for candidate in (root, *root.parents):
            if (candidate / ".git").exists():
                canonical = candidate
                break
        identity = str(canonical)
        if os.name == "nt":
            identity = identity.casefold()
        project_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return cls(MEMORY_DIR / "projects" / project_key)

    @staticmethod
    def _bounded_index(content: str) -> str:
        selected: list[str] = []
        size = 0
        for line in content.splitlines()[:MEMORY_INDEX_MAX_LINES]:
            encoded = (line + "\n").encode("utf-8")
            if size + len(encoded) > MEMORY_INDEX_MAX_BYTES:
                break
            selected.append(line)
            size += len(encoded)
        return "\n".join(selected).strip()

    def _ensure_initialized(self) -> None:
        """确保记忆目录和索引文件存在。"""
        self._dir.mkdir(parents=True, exist_ok=True)

        if not self._index_file.exists():
            atomic_write_text(self._index_file, DEFAULT_MEMORY_INDEX)
            logger.info("已创建默认 MEMORY.md 索引")

        # 创建默认记忆文件（如果不存在）
        for filename, content in DEFAULT_MEMORY_FILES.items():
            filepath = self._dir / filename
            if not filepath.exists():
                atomic_write_text(filepath, content)

    def get_index(self) -> str:
        """
        获取 MEMORY.md 索引内容。

        启动时注入 context 的是这个索引，不是正文。
        每条 ≤ 80 chars，总量约 500 tokens。
        """
        try:
            return self._bounded_index(self._index_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("读取 MEMORY.md 索引失败: %s", exc)
            return "（记忆索引不可用）"

    def read_file(self, filename: str) -> str | None:
        """
        读取具体记忆文件内容。

        Args:
            filename: 文件名（如 user_profile.md），不含路径

        Returns:
            文件内容，不存在返回 None
        """
        # 安全检查：防止路径遍历
        safe_name = Path(filename).name
        filepath = self._dir / safe_name

        if not filepath.exists():
            return None

        try:
            return filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("读取记忆文件 %s 失败: %s", filename, exc)
            return None

    def file_age_days(self, filename: str) -> float | None:
        """Return how many days since *filename* was last written.

        Uses the file's mtime — zero extra storage, and it advances naturally
        every time save_file rewrites the memory. Returns None when the file
        does not exist. Callers use this to attach a staleness warning so the
        model re-verifies old memories against the live workspace before
        trusting them.
        """
        safe_name = Path(filename).name
        filepath = self._dir / safe_name
        if not filepath.exists():
            return None
        try:
            age_seconds = max(0.0, time.time() - filepath.stat().st_mtime)
        except OSError as exc:
            logger.warning("读取记忆文件 %s 修改时间失败: %s", filename, exc)
            return None
        return age_seconds / 86400.0

    def save_file(self, filename: str, content: str) -> bool:
        """
        写入/更新记忆文件。

        Args:
            filename: 文件名（如 user_profile.md），不含路径
            content: 要写入的内容

        Returns:
            是否成功
        """
        safe_name = Path(filename).name
        filepath = self._dir / safe_name

        try:
            atomic_write_text(filepath, content)
            logger.info("已更新记忆文件: %s", safe_name)
            return True
        except OSError as exc:
            logger.error("写入记忆文件 %s 失败: %s", filename, exc)
            return False

    def list_files(self) -> list[str]:
        """列出所有记忆文件名。"""
        if not self._dir.exists():
            return []
        return [
            f.name
            for f in sorted(self._dir.iterdir())
            if f.is_file() and f.suffix == ".md"
        ]

    def update_index_entry(self, filename: str, description: str) -> None:
        """
        更新 MEMORY.md 索引中的某条描述。

        如果该文件已有索引行，更新描述；否则追加。
        """
        index_content = self.get_index()
        lines = index_content.split("\n")

        # 查找已有行
        target_prefix = f"- [{Path(filename).stem}]"
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(target_prefix):
                lines[i] = f"- [{Path(filename).stem}]({filename}) — {description}"
                found = True
                break

        if not found:
            lines.append(f"- [{Path(filename).stem}]({filename}) — {description}")

        bounded = self._bounded_index("\n".join(lines))
        atomic_write_text(self._index_file, f"{bounded}\n" if bounded else "")
