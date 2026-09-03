"""Bundled MiniCode memory prompts and fail-closed template rendering."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from backend.memory.text_utils import truncate_middle_tokens as _truncate_middle_tokens


TEMPLATE_ROOT = Path(__file__).with_name("templates")
PHASE2_WORKSPACE_DIFF_FILE = "phase2_workspace_diff.md"
MEMORY_SUMMARY_TOKEN_LIMIT = 2_500
_PLACEHOLDER_RE = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")

_EXTENSIONS_FOLDER_STRUCTURE = """
Memory extensions (under {{ memory_extensions_root }}/):

- <extension_name>/instructions.md
  - Source-specific guidance for interpreting additional memory signals. If an
    extension folder exists, you must read its instructions.md to determine how to use this memory
    source.

If the user has any memory extensions, you MUST read the instructions for each extension to
determine how to use the memory source. If the workspace diff shows deleted extension resource files,
remove stale memories derived only from those resources. If it has no extension folders, continue
with the standard memory inputs only.
"""

_EXTENSIONS_PRIMARY_INPUTS = """
Optional source-specific inputs:
Under `{{ memory_extensions_root }}/`:

- `<extension_name>/instructions.md`
  - If extension folders exist, read each instructions.md first and follow it when interpreting
    that extension's memory source.

If the workspace diff shows deleted memory extension resources, use that extension-specific deletion
signal to remove stale memories derived only from those resources.
"""


def _load_template(name: str) -> str:
    path = TEMPLATE_ROOT / name
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to load bundled memory template: {path}") from exc


def _render_template(source: str, values: Mapping[str, str], *, name: str) -> str:
    placeholders = set(_PLACEHOLDER_RE.findall(source))
    supplied = set(values)
    missing = placeholders - supplied
    unknown = supplied - placeholders
    if missing or unknown:
        raise RuntimeError(
            f"Invalid {name} template values: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )

    rendered = _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], source)
    unresolved = _PLACEHOLDER_RE.findall(rendered)
    if unresolved:
        raise RuntimeError(
            f"Unresolved {name} template values: {sorted(set(unresolved))}"
        )
    return rendered


STAGE1_SYSTEM_PROMPT = _load_template("stage_one_system.md")
_STAGE1_INPUT_TEMPLATE = _load_template("stage_one_input.md")
_CONSOLIDATION_TEMPLATE = _load_template("consolidation.md")
_READ_PATH_TEMPLATE = _load_template("read_path.md")
AD_HOC_INSTRUCTIONS = _load_template("ad_hoc_instructions.md")


def build_stage1_input(
    *,
    rollout_path: str,
    rollout_cwd: str,
    rollout_contents: str,
) -> str:
    return _render_template(
        _STAGE1_INPUT_TEMPLATE,
        {
            "rollout_path": str(rollout_path),
            "rollout_cwd": str(rollout_cwd),
            "rollout_contents": str(rollout_contents),
        },
        name="stage-one input",
    )


def build_consolidation_prompt(memory_root: Path | str) -> str:
    root = Path(memory_root).expanduser().resolve()
    extensions_root = root / "extensions"
    extensions_exist = extensions_root.is_dir()
    extension_values = {"memory_extensions_root": str(extensions_root)}
    return _render_template(
        _CONSOLIDATION_TEMPLATE,
        {
            "memory_root": str(root),
            "memory_extensions_folder_structure": (
                _render_template(
                    _EXTENSIONS_FOLDER_STRUCTURE,
                    extension_values,
                    name="memory extensions folder structure",
                )
                if extensions_exist
                else ""
            ),
            "memory_extensions_primary_inputs": (
                _render_template(
                    _EXTENSIONS_PRIMARY_INPUTS,
                    extension_values,
                    name="memory extensions primary inputs",
                )
                if extensions_exist
                else ""
            ),
            "phase2_workspace_diff_file": PHASE2_WORKSPACE_DIFF_FILE,
        },
        name="consolidation",
    )


def build_memory_read_prompt(memory_root: Path | str, memory_summary: str) -> str:
    summary = _truncate_middle_tokens(
        str(memory_summary).strip(),
        MEMORY_SUMMARY_TOKEN_LIMIT,
    )
    if not summary:
        return ""
    return _render_template(
        _READ_PATH_TEMPLATE,
        {
            "base_path": str(Path(memory_root).expanduser().resolve()),
            "memory_summary": summary,
        },
        name="memory read path",
    )
