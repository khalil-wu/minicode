"""Render the always-on Codex-style Skill catalog."""

from __future__ import annotations

import logging

from backend.skills.manager import SkillManager

logger = logging.getLogger(__name__)

# Codex-style progressive disclosure: the always-on skill index must stay small
# so selecting one skill can still load its full SKILL.md later.
LAYER1_SUMMARY_MAX_CHARS = 8000

class SkillExecutor:
    """Keep lightweight Skill discovery separate from turn-scoped injection."""

    def __init__(self, skill_manager: SkillManager) -> None:
        self._manager = skill_manager

    def build_layer1_summary(self, max_chars: int = LAYER1_SUMMARY_MAX_CHARS) -> str:
        """
        构建 Layer 1 摘要（始终注入）。

        Let the model discover available Skills before an exact SKILL.md is
        selected and injected as contextual user input.

        Args:
            max_chars: Layer 1 metadata budget. Defaults to the Codex-like
                cap of 8000 chars (roughly 2k tokens).

        Returns:
            Skill 摘要列表文本
        """
        summary = self._manager.get_layer1_summary()
        if not summary:
            return ""

        summary = _cap_layer1_summary(summary, max_chars=max_chars)
        return (
            "\n\n## Skills\n"
            "A skill is a set of instructions provided through a `SKILL.md` source. Below is the list of skills that can be used. Each entry includes a name, description, and source locator.\n"
            "### Available skills\n"
            + summary
            + "\n### How to use skills\n"
            "- Trigger rules: If the user names a skill (with `$SkillName` or plain text) OR the task clearly matches a skill's description shown above, you must use that skill for that turn. Multiple mentions mean use them all. Do not carry skills across turns unless re-mentioned.\n"
            "- After deciding to use a skill, read its `SKILL.md` completely before taking task actions. Resolve referenced files relative to that skill directory and load only the references needed for the task.\n"
            "- Prefer provided scripts, assets, and templates over recreating them. Normal tool permissions still apply.\n"
            "- If a named skill is missing or cannot be read, say so briefly and continue with the best fallback."
        )

def _cap_layer1_summary(summary: str, *, max_chars: int) -> str:
    """Cap the always-injected skill index without splitting mid-entry."""
    if max_chars <= 0:
        return ""
    if len(summary) <= max_chars:
        return summary

    notice_template = "\n- ... {omitted} more skill entries omitted by context budget."
    lines = [line for line in summary.splitlines() if line.strip()]
    kept: list[str] = []
    used = 0
    omitted = 0

    for index, line in enumerate(lines):
        remaining_after_this = len(lines) - index - 1
        notice = notice_template.format(omitted=max(1, remaining_after_this))
        line_len = len(line) + (1 if kept else 0)
        if used + line_len + len(notice) > max_chars:
            omitted = len(lines) - index
            break
        kept.append(line)
        used += line_len

    if kept:
        notice = notice_template.format(omitted=omitted or max(1, len(lines) - len(kept)))
        capped = "\n".join(kept) + notice
        return capped[:max_chars]

    fallback_notice = notice_template.format(omitted=max(1, len(lines))).lstrip()
    head_budget = max(0, max_chars - len(fallback_notice) - 1)
    return f"{summary[:head_budget].rstrip()}\n{fallback_notice}"[:max_chars]
