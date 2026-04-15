"""
文本分块策略（DESIGN.md §四.3）。

三种分块模式：
  1. 通用文档: 固定 512 tokens，64 tokens 重叠
  2. 代码文件: 按函数/类定义边界分割
  3. 对话历史: 按话题转换分割（双换行 / 角色切换）

分块质量直接影响 RAG 检索效果，过大则噪声多，过小则丢失上下文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ChunkMode(Enum):
    """分块模式。"""
    GENERAL = "general"         # 通用文档
    CODE = "code"               # 代码文件
    CONVERSATION = "conversation"  # 对话历史


@dataclass
class Chunk:
    """一个文本块。"""
    content: str
    metadata: dict[str, str | int]  # 来源信息（文件名、行号等）

    @property
    def token_estimate(self) -> int:
        return len(self.content) // 4


class Chunker:
    """
    文本分块器。

    使用示例：
        chunker = Chunker()
        chunks = chunker.chunk(text, mode=ChunkMode.GENERAL, source="doc.md")
        chunks = chunker.chunk_code(code, file_path="main.py")
    """

    def __init__(
        self,
        chunk_size: int = 512,     # tokens
        chunk_overlap: int = 64,    # tokens
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        # 估算：1 token ≈ 4 chars
        self._char_size = chunk_size * 4
        self._char_overlap = chunk_overlap * 4

    def chunk(
        self,
        text: str,
        mode: ChunkMode = ChunkMode.GENERAL,
        source: str = "",
    ) -> list[Chunk]:
        """
        分块入口。

        Args:
            text: 输入文本
            mode: 分块模式
            source: 来源标识

        Returns:
            Chunk 列表
        """
        if mode == ChunkMode.CODE:
            return self.chunk_code(text, file_path=source)
        elif mode == ChunkMode.CONVERSATION:
            return self._chunk_conversation(text, source)
        else:
            return self._chunk_general(text, source)

    def _chunk_general(self, text: str, source: str) -> list[Chunk]:
        """
        通用文档分块。

        策略：
          1. 优先按段落（双换行）分割
          2. 大段落按 chunk_size 滑动窗口切分
          3. 保留 overlap 以维护上下文连贯性
        """
        paragraphs = re.split(r"\n\n+", text)
        chunks: list[Chunk] = []
        buffer = ""
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # 如果单段落就超过 chunk_size
            if len(para) > self._char_size:
                # 先保存 buffer
                if buffer.strip():
                    chunks.append(Chunk(
                        content=buffer.strip(),
                        metadata={"source": source, "chunk_index": chunk_idx},
                    ))
                    chunk_idx += 1
                    buffer = ""

                # 滑动窗口切分大段落
                for i in range(0, len(para), self._char_size - self._char_overlap):
                    chunk_text = para[i:i + self._char_size]
                    if chunk_text.strip():
                        chunks.append(Chunk(
                            content=chunk_text.strip(),
                            metadata={"source": source, "chunk_index": chunk_idx},
                        ))
                        chunk_idx += 1
                continue

            # 检查 buffer + para 是否超过上限
            candidate = buffer + "\n\n" + para if buffer else para
            if len(candidate) > self._char_size:
                # 保存当前 buffer
                if buffer.strip():
                    chunks.append(Chunk(
                        content=buffer.strip(),
                        metadata={"source": source, "chunk_index": chunk_idx},
                    ))
                    chunk_idx += 1
                    # 保留 overlap
                    overlap_text = buffer[-self._char_overlap:] if len(buffer) > self._char_overlap else ""
                    buffer = overlap_text + "\n\n" + para
                else:
                    buffer = para
            else:
                buffer = candidate

        # 保存最后的 buffer
        if buffer.strip():
            chunks.append(Chunk(
                content=buffer.strip(),
                metadata={"source": source, "chunk_index": chunk_idx},
            ))

        return chunks

    def chunk_code(self, code: str, file_path: str = "") -> list[Chunk]:
        """
        代码文件分块。

        策略：按函数/类定义边界分割。
        """
        lines = code.split("\n")
        ext = Path(file_path).suffix.lower() if file_path else ".py"

        # 找出函数/类边界
        boundaries = self._find_boundaries(lines, ext)
        chunks: list[Chunk] = []

        if not boundaries or len(boundaries) == 1:
            # 无明确边界，使用通用分块
            return self._chunk_general(code, source=file_path)

        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
            chunk_text = "\n".join(lines[start:end])

            if chunk_text.strip():
                chunks.append(Chunk(
                    content=chunk_text.strip(),
                    metadata={
                        "source": file_path,
                        "start_line": start + 1,
                        "end_line": end,
                        "chunk_index": i,
                    },
                ))

        return chunks

    def _chunk_conversation(self, text: str, source: str) -> list[Chunk]:
        """
        对话历史分块。

        策略：按角色切换（user/assistant）或话题分隔符分割。
        """
        # 按角色标记或双换行分割
        segments = re.split(r"(?:^|\n)(?=(?:user|assistant|Human|AI|用户|助手)\s*:)", text)

        chunks: list[Chunk] = []
        for i, seg in enumerate(segments):
            seg = seg.strip()
            if not seg:
                continue

            # 如果段太长，进一步分割
            if len(seg) > self._char_size:
                sub_chunks = self._chunk_general(seg, source)
                chunks.extend(sub_chunks)
            else:
                chunks.append(Chunk(
                    content=seg,
                    metadata={"source": source, "chunk_index": i},
                ))

        return chunks

    @staticmethod
    def _find_boundaries(lines: list[str], ext: str) -> list[int]:
        """找出代码边界行号。"""
        patterns = {
            ".py": [r"^(?:class|def|async\s+def)\s+\w+"],
            ".js": [r"^(?:function|class|const|let|export)\s+\w+"],
            ".ts": [r"^(?:function|class|const|let|interface|type|export)\s+\w+"],
            ".tsx": [r"^(?:function|class|const|let|interface|type|export)\s+\w+"],
            ".go": [r"^(?:func|type)\s+"],
            ".rs": [r"^(?:fn|struct|enum|impl|trait|pub\s+fn)\s+"],
        }

        lang_patterns = patterns.get(ext, [r"^(?:function|class|def)\s+\w+"])
        boundaries = [0]

        for i, line in enumerate(lines):
            stripped = line.strip()
            for pattern in lang_patterns:
                if re.match(pattern, stripped):
                    if i not in boundaries:
                        boundaries.append(i)
                    break

        return boundaries
