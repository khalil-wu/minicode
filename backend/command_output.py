"""Compact command output without hiding the failure that matters.

Long test/build output is usually dominated by collection logs and repeated
tracebacks.  A plain head-only or tail-only truncation forces the agent to spend
extra turns reading an artifact before it can see the failing check.  This
module keeps deterministic failure identifiers plus a few detailed excerpts,
while still bounding the text injected into model context.
"""

from __future__ import annotations

import re


_FAILURE_SUMMARY_RE = re.compile(
    r"(?i)^(?:"
    r"FAIL(?:ED)?(?:\s|:)|"
    r"ERROR(?:\s|:)|"
    r"E\s{2,}|"
    r"AssertionError\b|"
    r"FAILED\s*\(|"
    r"Ran\s+\d+\s+tests?\b|"
    r"=+\s*(?:FAILURES|ERRORS|short test summary info)\s*=+"
    r")"
)


def _dedupe_preserving_order(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        normalized = line.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(line.rstrip())
    return result


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n... [excerpt clipped] ...\n"
    usable = max(0, limit - len(marker))
    head = usable // 2
    return f"{text[:head]}{marker}{text[-(usable - head):]}"


def compact_command_output(
    output: str,
    *,
    failed: bool,
    max_chars: int = 6_000,
) -> str:
    """Return a bounded, failure-focused command excerpt.

    Successful output keeps its beginning and end.  Failed output additionally
    lists every detected failing check (within the bound) and includes detailed
    context around the first few failure anchors.  The function is deliberately
    deterministic and does not infer whether the task itself succeeded.
    """

    text = str(output or "").strip()
    limit = max(256, int(max_chars or 0))
    if len(text) <= limit:
        return text

    marker = f"[command output compacted from {len(text)} chars]"
    head_budget = min(900, max(300, limit // 6))
    tail_budget = min(1_800, max(600, limit // 3))
    head = text[:head_budget].rstrip()
    tail = text[-tail_budget:].lstrip()

    if not failed:
        return _clip(
            f"{marker}\n\n--- beginning ---\n{head}\n\n--- end ---\n{tail}",
            limit,
        )

    lines = text.splitlines()
    anchors = [index for index, line in enumerate(lines) if _FAILURE_SUMMARY_RE.search(line.strip())]
    summaries = _dedupe_preserving_order([lines[index] for index in anchors])[:24]

    detail_blocks: list[str] = []
    used_ranges: list[tuple[int, int]] = []
    for index in anchors:
        start = max(0, index - 2)
        end = min(len(lines), index + 11)
        if any(start < prior_end and end > prior_start for prior_start, prior_end in used_ranges):
            continue
        used_ranges.append((start, end))
        detail_blocks.append("\n".join(lines[start:end]).strip())
        if len(detail_blocks) >= 4:
            break

    # Put the decisive failure evidence first so a final defensive clip cannot
    # discard it in favor of setup noise from the beginning of the command.
    sections = [marker]
    if summaries:
        sections.append("--- detected failing checks ---\n" + "\n".join(summaries))
    if detail_blocks:
        sections.append("--- failure details ---\n" + "\n\n".join(detail_blocks))
    sections.append(f"--- beginning ---\n{head}")
    sections.append(f"--- end ---\n{tail}")
    return _clip("\n\n".join(sections), limit)
