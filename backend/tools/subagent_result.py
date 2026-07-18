from __future__ import annotations

import re

from backend.tools.base import truncate_tool_result

SUBAGENT_REPORT_INSTRUCTIONS = """
Final response contract for this delegated task:
- Return a coordinator-ready report, not a tool log or narration.
- Use the same language as the assigned task when practical.
- Use these Markdown sections, in this order:
  ## Result
  - State the verdict or outcome in 1-3 bullets.
   ## Evidence
   - Include concrete evidence, file paths, line numbers, commands, or observations.
   ## Evidence claims
   - For external/current facts, return a fenced JSON array. Each item must contain subject, field, value, unit, valid_at, confidence, verdict, and sources (url, title, retrieved_at).
   - Use verdict "accepted" only for a value verified from a primary source; otherwise use "unverified". Return [] when no structured external fact is claimed.
  ## Changes
  - List files changed, or write "- None." for read-only work.
  ## Verification
  - List checks run and their outcome. For verification agents, start with VERDICT: PASS, VERDICT: FAIL, or VERDICT: PARTIAL.
  ## Risks or blockers
  - Call out unresolved risks, missing context, or write "- None."
- Keep the report concise. Do not include raw command output, tool-call logs, repeated progress narration, or plans for future polling.
- For external or current facts, keep each adopted value attached to its source URL and validity date. Do not silently replace a value with a conflicting search snippet.
- If sources disagree, list the conflict under Risks or blockers and mark the affected value unverified; do not choose one without explaining the evidence basis.
""".strip()

SIMPLIFIED_CHINESE_OUTPUT_INSTRUCTION = (
    "所有可见进展、标题、错误信息和最终结果都必须使用简体中文"
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,4})\s+(.+?)\s*$")
_IMPORTANT_HEADING_RE = re.compile(
    r"(result|summary|conclusion|finding|evidence|claim|change|verification|risk|blocker|"
    r"结论|结果|发现|证据|变更|验证|风险|阻塞)",
    re.IGNORECASE,
)


def append_subagent_report_contract(prompt: str) -> str:
    text = str(prompt or "").strip()
    additions: list[str] = []
    if _CJK_RE.search(text) and SIMPLIFIED_CHINESE_OUTPUT_INSTRUCTION not in text:
        additions.append(SIMPLIFIED_CHINESE_OUTPUT_INSTRUCTION)
    if SUBAGENT_REPORT_INSTRUCTIONS not in text:
        additions.append(SUBAGENT_REPORT_INSTRUCTIONS)
    if not additions:
        return text
    addition_text = "\n\n".join(additions)
    return f"{text}\n\n{addition_text}".strip()


def compact_subagent_result(content: str, *, max_chars: int = 4_000) -> tuple[str, bool]:
    text = str(content or "").strip()
    if not text:
        return "", False
    max_chars = max(800, min(int(max_chars or 4_000), 12_000))
    if len(text) <= max_chars:
        return text, False

    structured = _important_markdown_sections(text)
    if structured:
        compact = _fit_with_notice(
            structured,
            max_chars=max_chars,
            notice="\n\n[Full delegated result omitted. Call task_status with detail_level=\"full\" if raw detail is needed.]",
        )
        return compact, True

    fallback = truncate_tool_result(text, max_chars)
    fallback = _fit_with_notice(
        fallback,
        max_chars=max_chars,
        notice="\n\n[Long delegated result summarized by truncation. Call task_status with detail_level=\"full\" if raw detail is needed.]",
    )
    return fallback, True


def full_subagent_result(content: str, *, max_chars: int = 12_000) -> tuple[str, bool]:
    text = str(content or "").strip()
    max_chars = max(1_200, min(int(max_chars or 12_000), 24_000))
    if len(text) <= max_chars:
        return text, False
    return truncate_tool_result(text, max_chars), True


def _important_markdown_sections(text: str) -> str:
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    preface: list[str] = []

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current_title:
                sections.append((current_title, current_lines))
            elif current_lines:
                preface.extend(current_lines)
            current_title = match.group(2).strip()
            current_lines = [line]
            continue
        current_lines.append(line)

    if current_title:
        sections.append((current_title, current_lines))
    elif current_lines:
        preface.extend(current_lines)

    selected: list[str] = []
    for title, lines in sections:
        if _IMPORTANT_HEADING_RE.search(title):
            selected.append("\n".join(lines).strip())

    if selected:
        return "\n\n".join(section for section in selected if section).strip()

    clean_preface = "\n".join(_non_log_lines(preface)).strip()
    return clean_preface


def _non_log_lines(lines: list[str]) -> list[str]:
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if re.match(r"^Subagent\s+subagent-[\w-]+.*completed", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Tools used \(\d+ total\):", stripped, re.IGNORECASE):
            continue
        if re.match(r"^[-*]\s*\w+_?\w*\(.*\)\s*\[[^\]]+\]$", stripped):
            continue
        kept.append(line)
    return kept


def _fit_with_notice(text: str, *, max_chars: int, notice: str) -> str:
    body_limit = max(200, max_chars - len(notice))
    body = text[:body_limit].rstrip()
    if len(text) > body_limit:
        body += "\n..."
    return f"{body}{notice}".strip()
