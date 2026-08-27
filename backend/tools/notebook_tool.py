"""Notebook editing tool — edit Jupyter (.ipynb) cells.

Cells are addressed by ``cell_id`` (exact id or ``cell-N`` numeric form) and
carry ``new_source``. A replace reuses the target cell's id so a later call can
re-address it.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from backend.atomic_io import canonical_path_mapping_key, file_mutation_locks
from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.file_tools_common import _atomic_write_text, _validate_expected_hash, content_hash
from backend.tools.path_resolution import _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache

_CELL_ID_RE = re.compile(r"^cell-(\d+)$")


def _parse_cell_id(cell_id: str) -> int | None:
    match = _CELL_ID_RE.match(cell_id)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


class NotebookEditTool(BaseTool):
    """Edit a cell in a Jupyter notebook (.ipynb)."""

    name = "notebook_edit"
    result_kind = "edit"
    activity_kind = "fileChange"
    display_label = "Edit notebook"
    mutates_workspace = True
    read_only = False
    permission = PermissionLevel.CONFIRM
    workspace_path_fields = ("notebook_path",)
    description = (
        "Edit a Jupyter notebook (.ipynb) cell by its cell_id (the cell's id, or a "
        "cell-N numeric reference). Supports replace / insert / delete. When inserting, "
        "the new cell is placed after the referenced cell (or at the top when omitted); "
        "use cell_type to set code or markdown. For plain files prefer write_file/edit_file."
    )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "notebook_path": {"type": "string", "description": "Path to the .ipynb file."},
                    "cell_id": {
                        "type": "string",
                        "description": (
                            "The id of the cell to edit (or a cell-N reference). "
                            "When inserting, the new cell is inserted after this cell, or at the "
                            "beginning when omitted."
                        ),
                    },
                    "new_source": {"type": "string", "description": "The new source for the cell."},
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown"],
                        "description": "Cell type for insert, or to change an existing cell's type.",
                    },
                    "edit_mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": "replace (default), insert after cell_id, or delete cell_id.",
                    },
                    # Legacy aliases kept so existing transcripts still resolve.
                    "cell_index": {
                        "type": "integer",
                        "description": "Deprecated alias for cell_id, as a 0-based index.",
                    },
                    "source": {"type": "string", "description": "Deprecated alias for new_source."},
                },
                "required": ["notebook_path", "new_source"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        raw_path = str(args.get("notebook_path") or "").strip()
        if not raw_path:
            return self._error_result("notebook_path is required")
        try:
            path = _resolve_path(raw_path, context, allow_workspace_escape=_is_bypass_mode(context))
        except Exception as exc:
            return self._error_result(f"Invalid notebook path: {exc}")
        if not path.exists():
            return self._error_result(f"Notebook not found: {raw_path}")
        if path.suffix.lower() != ".ipynb":
            return self._error_result(f"Not a notebook (.ipynb): {raw_path}")

        # Same write floor as write_file/edit_file: a notebook inside .git/ or
        # .minicode/ can execute on open, so it must not be a way around the
        # protected-path guard. This stays enforced even in bypass mode.
        if is_protected_write_path(path):
            return self._error_result(
                f"Refusing to write protected path: {raw_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        # Read-before-edit + staleness guard (mirrors edit_file.py). Notebook
        # freshness comes from the content hash read_file recorded in context
        # metadata rather than a model-supplied one. Honor an explicit expected_hash
        # if the model passed one, otherwise fall back to the recorded read-time
        # hash so a notebook read this session is editable without the model
        # echoing the hash back (edit_file/write_file get the same hash injected
        # via generate_diff; notebook_edit resolves it here instead).
        expected_hash = args.get("expected_hash")
        if not expected_hash and context is not None and isinstance(getattr(context, "metadata", None), dict):
            read_time_hashes = context.metadata.get("_read_file_hashes")
            if isinstance(read_time_hashes, dict):
                expected_hash = read_time_hashes.get(canonical_path_mapping_key(read_time_hashes, path))
        ok, message = _validate_expected_hash(path, expected_hash)
        if not ok:
            return self._error_result(message)

        try:
            nb = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return self._error_result(f"Failed to read notebook JSON: {exc}")
        cells = nb.get("cells")
        if not isinstance(cells, list):
            return self._error_result("Notebook has no cells array")

        edit_mode = str(args.get("edit_mode") or "replace").strip().lower()
        if edit_mode not in {"replace", "insert", "delete"}:
            return self._error_result("edit_mode must be replace, insert, or delete")

        # Resolve the target index from cell_id (exact id, then cell-N numeric),
        # with a legacy cell_index alias.
        cell_id = args.get("cell_id")
        cell_index_arg = args.get("cell_index")
        idx: int | None = None
        if cell_index_arg is not None:
            try:
                idx = int(cell_index_arg)
            except (TypeError, ValueError):
                return self._error_result("cell_index must be an integer")
        elif cell_id:
            cell_id = str(cell_id)
            idx = next((i for i, c in enumerate(cells) if c.get("id") == cell_id), None)
            if idx is None:
                parsed = _parse_cell_id(cell_id)
                if parsed is not None:
                    idx = parsed
            if idx is None:
                return self._error_result(f'Cell with ID "{cell_id}" not found in notebook.')
        elif edit_mode != "insert":
            return self._error_result("cell_id must be specified when not inserting a new cell.")

        new_source = args.get("new_source", args.get("source"))
        if edit_mode == "delete":
            if idx is None or not 0 <= idx < len(cells):
                return self._error_result(f"cell_id out of range (0..{len(cells) - 1})")
            cells.pop(idx)
            action = f"Deleted cell {idx}"
        else:
            if new_source is None:
                return self._error_result("new_source is required for replace/insert")
            if edit_mode == "insert":
                idx = (idx + 1) if idx is not None else 0
                if not 0 <= idx <= len(cells):
                    return self._error_result(f"cell_id out of range (0..{len(cells)})")
                cell_type = str(args.get("cell_type") or "code").strip().lower()
                if cell_type not in {"code", "markdown"}:
                    return self._error_result(f"Invalid cell_type '{cell_type}'")
                new_cell = self._build_cell(cell_type, str(new_source))
                cells.insert(idx, new_cell)
                action = f"Inserted {cell_type} cell at {idx}"
            else:  # replace
                if idx == len(cells):
                    # Replace one past the end is an append.
                    cell_type = str(args.get("cell_type") or "code").strip().lower()
                    if cell_type not in {"code", "markdown"}:
                        return self._error_result(f"Invalid cell_type '{cell_type}'")
                    cells.append(self._build_cell(cell_type, str(new_source)))
                    action = f"Appended {cell_type} cell"
                else:
                    if not 0 <= idx < len(cells):
                        return self._error_result(f"cell_id out of range (0..{len(cells) - 1})")
                    target = cells[idx]
                    target["source"] = str(new_source).splitlines(keepends=True)
                    if target.get("cell_type") == "code":
                        target["execution_count"] = None
                        target["outputs"] = []
                    cell_type = str(args.get("cell_type") or "").strip().lower()
                    if cell_type in {"code", "markdown"}:
                        target["cell_type"] = cell_type
                    # Reuse the existing id so a later call can re-address this cell.
                    action = f"Replaced cell {target.get('id')}"

        nb["cells"] = cells
        try:
            new_text = json.dumps(nb, ensure_ascii=False, indent=1)
            # Notebook edits share the same guarded mutation queue as text
            # edits. Revalidate after waiting so a concurrent editor save is
            # rejected instead of silently overwritten.
            with file_mutation_locks([path]):
                ok, message = _validate_expected_hash(path, expected_hash)
                if not ok:
                    return self._error_result(message)
                _atomic_write_text(path, new_text)

                # Keep cache publication inside the queue; the event/result can
                # be emitted after release because it does not affect the file.
                try:
                    get_global_file_cache().invalidate(path)
                    from backend.tools.file_tools_common import invalidate_workspace_file_caches

                    invalidate_workspace_file_caches()
                except Exception:
                    pass
        except Exception as exc:
            return self._error_result(f"Failed to write notebook: {exc}")

        return self._success_result(
            content=f"{action} in {raw_path} ({len(cells)} cells). content_hash: {content_hash(new_text)}",
            display_summary=action,
        )

    @staticmethod
    def _build_cell(cell_type: str, source: str) -> dict[str, Any]:
        """Build a new cell. nbformat 4.5+ requires a unique id matching
        ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$; older minor versions tolerate it."""
        new_cell: dict[str, Any] = {
            "id": f"c{uuid.uuid4().hex[:12]}",
            "cell_type": cell_type,
            "metadata": {},
            "source": source.splitlines(keepends=True),
        }
        if cell_type == "code":
            new_cell["execution_count"] = None
            new_cell["outputs"] = []
        return new_cell
