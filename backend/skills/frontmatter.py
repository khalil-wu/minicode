"""SKILL.md metadata shared by discovery and installation."""

from __future__ import annotations

import re
from copy import deepcopy
from functools import lru_cache
from typing import Any

import yaml


@lru_cache(maxsize=256)
def _parse_yaml_contents(contents: str):
    return yaml.safe_load(contents)


def parse_skill_yaml(contents: str):
    """Reuse unchanged syntax while every catalog owns its decoded metadata."""
    return deepcopy(_parse_yaml_contents(contents))


def parse_skill_frontmatter(content: str, fallback_name: str) -> dict[str, Any]:
    match = re.match(
        r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
        content,
        re.DOTALL,
    )
    if match is None:
        raise ValueError("SKILL.md requires YAML frontmatter")
    try:
        metadata = parse_skill_yaml(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid SKILL.md YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter must be an object")
    raw_name = metadata.get("name")
    metadata["name"] = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else fallback_name
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("SKILL.md requires a description")
    metadata["description"] = description.strip()
    title = metadata.get("title")
    metadata["title"] = title.strip() if isinstance(title, str) and title.strip() else metadata["name"]
    return metadata
