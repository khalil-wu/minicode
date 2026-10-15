"""Source skills preferences and ordered name/path enablement rules."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillsConfiguration:
    include_instructions: bool = True
    bundled_enabled: bool = True
    max_context_tokens: int | None = None
    rules: tuple[tuple[str, str, bool], ...] = ()


def _parse_options(raw: Mapping[str, Any]) -> tuple[bool, bool, int | None]:
    include = raw.get("include_instructions")
    include = True if include is None else include
    bundled_options = raw.get("bundled")
    bundled = True if bundled_options is None else bundled_options.get("enabled", True)
    maximum = raw.get("max_context_tokens")
    if not isinstance(include, bool) or not isinstance(bundled, bool):
        raise ValueError("skills include_instructions/bundled.enabled must be booleans")
    if maximum is not None and (type(maximum) is not int or maximum <= 0):
        raise ValueError("skills.max_context_tokens must be a positive integer")
    return include, bundled, maximum


def skills_configuration_from_stack(stack: Any) -> SkillsConfiguration:
    options = stack.effective_config().get("skills", {})
    include, bundled, maximum = _parse_options(options)
    rules: list[tuple[str, str, bool]] = []
    for layer in stack.get_layers():
        raw = layer.config.get("skills", {})
        for entry in raw.get("config", []):
            enabled = entry["enabled"]
            if not isinstance(enabled, bool):
                raise ValueError("skills.config.enabled must be a boolean")
            path, name = entry.get("path"), entry.get("name")
            if (path is None) == (name is None) or (name is not None and not name.strip()):
                logger.warning("Ignoring skills.config entry without exactly one non-empty path or name selector")
                continue
            if path is not None:
                value = Path(path).expanduser()
                if not value.is_absolute():
                    directory = layer.source.project_config_folder or (str(Path(layer.source.file).parent) if layer.source.file else "")
                    value = Path(directory) / value
                selector = ("path", str(value.resolve()), enabled)
            else:
                selector = ("name", name.strip(), enabled)
            rules = [rule for rule in rules if rule[:2] != selector[:2]]
            rules.append(selector)
    return SkillsConfiguration(include, bundled, maximum, tuple(rules))
