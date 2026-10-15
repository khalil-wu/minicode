"""Render MiniCode's always-on Skill catalog."""

from __future__ import annotations

import logging
import json
from pathlib import Path

from backend.skills.manager import SkillManager
from backend.skills.loader import SkillMeta

logger = logging.getLogger(__name__)
_SOURCE_MESSAGES = json.loads(Path(__file__).with_name("source-catalog-instructions.json").read_text(encoding="utf-8"))["messages"]

# MiniCode uses one provider-neutral discovery contract. Provider wire formats
# must not change harness instructions or Skill visibility.
MINICODE_SKILL_BUDGET_CONTEXT_PERCENT = 0.02
CHARS_PER_TOKEN = 4
MINICODE_LAYER1_SUMMARY_MAX_CHARS = 8_000
MINICODE_MAX_LISTING_DESC_CHARS = 1_024


def utf8_prefix(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    end = min(len(encoded), maximum)
    while 0 < end < len(encoded) and encoded[end] & 0xC0 == 0x80:
        end -= 1
    return encoded[:end].decode("utf-8")


def _render_source_host_catalog(metas: list[SkillMeta], *, limit: int, token_budget: bool) -> str:
    rank = {"builtin": 0, "managed": 1, "workspace": 2, "user": 3, "plugin": 4}
    metas = sorted(metas, key=lambda meta: (rank.get(meta.source_level, 4), meta.name, str(meta.source_path)))
    paths = [str(meta.source_path).replace("\\", "/") for meta in metas]
    descriptions = [meta.description[:1021] + "..." if len(meta.description) > 1024 else meta.description for meta in metas]
    cost = (lambda value: (len(value.encode("utf-8")) + 3) // 4) if token_budget else len
    roots = list(dict.fromkeys(str(meta.alias_root).replace("\\", "/").rstrip("/") for meta in metas if meta.alias_root is not None))
    aliases = {root: f"r{index}" for index, root in enumerate(roots)}
    root_lines = [f"- `{alias}` = `{root}`" for root, alias in aliases.items()]
    short_paths = []
    for path in paths:
        root = max((root for root in roots if path.startswith(root + "/")), key=len, default=None)
        short_paths.append(aliases[root] + path[len(root):] if root is not None else path)
    plain_intro = _SOURCE_MESSAGES["SKILLS_INTRO_WITH_SOURCE_LOCATORS"]
    alias_intro = _SOURCE_MESSAGES["SKILLS_INTRO_WITH_HOST_ALIASES"]
    plain_suffix = _SOURCE_MESSAGES["SKILLS_HOW_TO_USE_WITH_SOURCE_LOCATORS"]
    alias_suffix = _SOURCE_MESSAGES["SKILLS_HOW_TO_USE_WITH_HOST_ALIASES"]
    def row(index: int, description: str, locators: list[str]) -> str:
        return f"- {metas[index].name}: {description} (file: {locators[index]})" if description else f"- {metas[index].name}: (file: {locators[index]})"
    alias_overhead = max(0, cost("### Skill roots\n" + "\n".join(root_lines) + "\n" + alias_intro) - cost(plain_intro))
    plain_cost = sum(cost(row(index, description, paths) + "\n") for index, description in enumerate(descriptions))
    alias_cost = alias_overhead + sum(cost(row(index, description, short_paths) + "\n") for index, description in enumerate(descriptions))
    aliased = bool(roots) and alias_cost < plain_cost
    locators = short_paths if aliased else paths
    available = max(0, limit - (alias_overhead if aliased else 0))
    full_rows = [row(index, description, locators) for index, description in enumerate(descriptions)]
    if sum(cost(line + "\n") for line in full_rows) <= available:
        rendered = full_rows
    else:
        minimum = [row(index, "", locators) for index in range(len(metas))]
        if sum(cost(line + "\n") for line in minimum) <= available:
            allocations = [0] * len(metas)
            used = sum(cost(line + "\n") for line in minimum)
            while used < available:
                changed = False
                for index, description in enumerate(descriptions):
                    count = allocations[index]
                    if count == len(description):
                        continue
                    delta = cost(row(index, description[:count + 1], locators) + "\n") - cost(row(index, description[:count], locators) + "\n")
                    if used + delta <= available:
                        allocations[index] += 1
                        used += delta
                        changed = True
                if not changed:
                    break
            rendered = [row(index, description[:allocations[index]], locators) for index, description in enumerate(descriptions)]
        else:
            rendered = []
            used = 0
            for line in minimum:
                charged = cost(line + "\n")
                if used + charged > available:
                    break
                rendered.append(line)
                used += charged
            logger.warning("Exceeded skills context budget. All skill descriptions were removed and %d additional skills were not included in the model-visible skills list.", len(metas) - len(rendered))
    lines = ["## Skills", alias_intro if aliased else plain_intro]
    if aliased:
        lines.extend(["### Skill roots", *root_lines])
    lines.extend(["### Available skills", *rendered, "### How to use skills", alias_suffix if aliased else plain_suffix])
    return "\n" + "\n".join(lines) + "\n"


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
        """Render Source host metadata separately from its full usage policy."""
        metas = [meta for meta in self._manager.list_metas() if meta.allow_implicit_invocation]
        if not metas or not self._manager.include_instructions:
            return ""
        configured = self._manager.max_context_tokens
        if max_chars is not None:
            limit, token_budget = max(0, int(max_chars)), False
            if limit == 0:
                return ""
        elif configured is not None:
            limit, token_budget = min(configured, 10_000), True
        elif context_window_tokens is not None and context_window_tokens > 0:
            limit, token_budget = max(1, context_window_tokens * 2 // 100), True
        else:
            limit, token_budget = 8_000, False
        return _render_source_host_catalog(metas, limit=limit, token_budget=token_budget)



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
    metas: list[SkillMeta],
    *,
    max_chars: int,
) -> str:
    """Render MiniCode's locator catalog with a hard metadata budget."""
    entries: list[tuple[str, str, str]] = []
    for meta in metas:
        name = meta.name
        path = str(meta.source_path).replace("\\", "/")
        description = meta.description
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
