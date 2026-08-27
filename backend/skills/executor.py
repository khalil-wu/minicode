"""Render MiniCode's always-on Skill catalog."""

from __future__ import annotations

import logging

from backend.skills.manager import SkillManager

logger = logging.getLogger(__name__)

# MiniCode uses one provider-neutral discovery contract. Provider wire formats
# must not change harness instructions or Skill visibility.
MINICODE_SKILL_BUDGET_CONTEXT_PERCENT = 0.02
CHARS_PER_TOKEN = 4
MINICODE_LAYER1_SUMMARY_MAX_CHARS = 16_000
MINICODE_MAX_LISTING_DESC_CHARS = 1_024


class SkillExecutor:
    """Keep lightweight Skill discovery separate from turn-scoped injection."""

    def __init__(self, skill_manager: SkillManager) -> None:
        self._manager = skill_manager

    def build_layer1_summary(
        self,
        max_chars: int | None = None,
        *,
        context_window_tokens: int | None = None,
    ) -> str:
        """
        构建 Layer 1 摘要（始终注入）。

        Let the model discover available Skills before an exact SKILL.md is
        selected and injected as contextual user input.

        Args:
            max_chars: Explicit metadata character budget. When omitted, use
                MiniCode's context-window policy.

        Returns:
            Skill 摘要列表文本
        """
        metas = [
            meta
            for meta in self._manager.list_metas()
            if meta.allow_implicit_invocation
        ]
        if not metas:
            return ""

        budget = _minicode_skill_char_budget(
            max_chars=max_chars,
            context_window_tokens=context_window_tokens,
        )
        summary = _format_minicode_skills_within_budget(metas, max_chars=budget)
        return (
            "\n\n## Skills\n"
            "A skill is a set of instructions provided through a `SKILL.md` source. Below is the list of skills that can be used. Each entry includes a name, description, and source locator.\n"
            "### Available skills\n"
            + summary
            + "\n### How to use skills\n"
            "- Trigger rules: If the user names a skill (with `$SkillName` or plain text) OR the task clearly matches a skill's description shown above, you must use that skill for that turn. Multiple mentions mean use them all. Do not carry skills across turns unless re-mentioned.\n"
            "- Missing/blocked: If a named skill is unavailable or its `SKILL.md` cannot be read, say so briefly and continue with the best fallback.\n"
            "- After deciding to use a skill, read its `SKILL.md` completely before taking task actions. Resolve relative references against the directory containing that `SKILL.md`.\n"
            "- Read every instruction or reference that the selected `SKILL.md` requires for the task. Load only relevant optional references and avoid unrelated or deep reference chasing.\n"
            "- Prefer provided scripts, assets, and templates over recreating them. Normal tool permissions still apply.\n"
            "- If multiple skills apply, use the smallest set that covers the request and state the order."
        )


def _minicode_skill_char_budget(
    *,
    max_chars: int | None,
    context_window_tokens: int | None,
) -> int:
    if max_chars is not None:
        return max(0, int(max_chars))
    if context_window_tokens:
        return max(
            1,
            int(
                context_window_tokens
                * CHARS_PER_TOKEN
                * MINICODE_SKILL_BUDGET_CONTEXT_PERCENT
            ),
        )
    return MINICODE_LAYER1_SUMMARY_MAX_CHARS


def _format_minicode_skills_within_budget(
    metas: list[object],
    *,
    max_chars: int,
) -> str:
    """Render MiniCode's locator catalog with a hard metadata budget."""
    entries: list[tuple[str, str, str]] = []
    for meta in metas:
        name = str(getattr(meta, "name", "") or "").strip()
        path = str(getattr(meta, "source_path", "") or "").replace("\\", "/")
        description = str(getattr(meta, "description", "") or "").strip()
        if len(description) > MINICODE_MAX_LISTING_DESC_CHARS:
            description = (
                description[: MINICODE_MAX_LISTING_DESC_CHARS - 3] + "..."
            )
        entries.append((name, description, path))

    full_lines = [
        f"- {name}: {description} (file: {path})"
        if description
        else f"- {name}: (file: {path})"
        for name, description, path in entries
    ]
    full = "\n".join(full_lines)
    if not entries or len(full) <= max_chars:
        return full
    if max_chars <= 0:
        return ""

    minimum_lines = [f"- {name}: (file: {path})" for name, _description, path in entries]
    minimum = "\n".join(minimum_lines)
    if len(minimum) > max_chars:
        return _whole_entries_with_omission_notice(minimum_lines, max_chars=max_chars)

    # Share the remaining description budget fairly so short descriptions
    # release their unused allocation to longer entries.
    remaining = max_chars - len(minimum)
    allocations = [0] * len(entries)
    while remaining > 0:
        changed = False
        for index, (_name, description, _path) in enumerate(entries):
            if allocations[index] >= len(description):
                continue
            cost = 2 if allocations[index] == 0 else 1
            if cost > remaining:
                continue
            allocations[index] += 1
            remaining -= cost
            changed = True
        if not changed:
            break

    rendered: list[str] = []
    for (name, description, path), count in zip(entries, allocations, strict=True):
        shown = description[:count]
        rendered.append(
            f"- {name}: {shown} (file: {path})"
            if shown
            else f"- {name}: (file: {path})"
        )
    return "\n".join(rendered)


def _whole_entries_with_omission_notice(lines: list[str], *, max_chars: int) -> str:
    if max_chars <= 0 or not lines:
        return ""
    total = len(lines)
    for included_count in range(total, -1, -1):
        omitted = total - included_count
        notice = f"- ... {omitted} skills omitted by context budget" if omitted else ""
        candidate = "\n".join([*lines[:included_count], *([notice] if notice else [])])
        if len(candidate) <= max_chars:
            return candidate
    return ""


def _cap_layer1_summary(summary: str, *, max_chars: int) -> str:
    """Hard-cap a pre-rendered MiniCode catalog at complete entry boundaries."""
    lines = [line for line in summary.splitlines() if line.strip()]
    if not lines or len(summary) <= max_chars:
        return summary
    if max_chars <= 0:
        return ""
    return _whole_entries_with_omission_notice(lines, max_chars=max_chars)
