"""
Tool argument JSON 修复工具。

当 LLM 输出的 tool_use 参数 JSON 不完整或格式错误时，
尝试常见修复策略，避免直接 fallback 到 {"_raw": ...}。
"""

from __future__ import annotations

import json
import re


def repair_tool_json(raw: str) -> dict | None:
    """尝试修复不完整的 JSON，返回 dict 或 None（无法修复）。"""
    if not raw or not raw.strip():
        return None

    stripped = raw.strip()

    # 1. 直接 parse
    try:
        result = json.loads(stripped)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. 移除 trailing comma
    cleaned = re.sub(r",\s*([}\]])", r"\1", stripped)
    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass

    # 3. 尝试闭合未完成的结构
    repaired = _try_close_json(stripped)
    if repaired is not None:
        return repaired

    # 4. 如果不以 { 开头，尝试包裹
    if not stripped.startswith("{"):
        try:
            result = json.loads("{" + stripped + "}")
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _try_close_json(raw: str) -> dict | None:
    """尝试通过闭合括号和引号来修复截断的 JSON。"""
    for trim in range(min(20, len(raw))):
        candidate = raw if trim == 0 else raw[:-trim]
        if not candidate.strip():
            continue

        # 计算未闭合的结构
        open_quotes = candidate.count('"') % 2
        open_braces = candidate.count("{") - candidate.count("}")
        open_brackets = candidate.count("[") - candidate.count("]")

        if open_braces < 0 or open_brackets < 0:
            continue

        patched = candidate
        if open_quotes:
            patched += '"'
        patched += "]" * open_brackets
        patched += "}" * open_braces

        # 移除 trailing comma before closing
        patched = re.sub(r",\s*([}\]])", r"\1", patched)

        try:
            result = json.loads(patched)
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, TypeError):
            continue

    return None
