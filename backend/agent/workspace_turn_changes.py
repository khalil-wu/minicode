"""The real workspace content boundary for one parent turn."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from backend.agent.turn_diff_tracker import TurnDiffTracker, ZERO_OID
from backend.security.sensitive_files import application_state_roots, is_protected_write_path
from backend.workspace.fuzzy_search import iter_search_paths


@dataclass(frozen=True, slots=True)
class _FileContent:
    oid: str
    text: str | None


def _read_workspace(root: Path, application_roots: tuple[Path, ...], write_scope: tuple[Path, ...] | None) -> dict[str, _FileContent]:
    contents: dict[str, _FileContent] = {}
    if not root.is_dir():
        return contents
    for path, is_directory in iter_search_paths(root, include_hidden=True):
        if is_directory or is_protected_write_path(path, application_roots=application_roots, resolved_path=path):
            continue
        if write_scope is not None and not any(path == scope or path.is_relative_to(scope) for scope in write_scope):
            continue
        raw = path.read_bytes()
        oid = hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
        text = None
        if b"\0" not in raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
        contents[path.relative_to(root).as_posix()] = _FileContent(oid, text)
    return contents


@dataclass(slots=True)
class WorkspaceTurnChanges:
    workspace_root: Path
    application_roots: tuple[Path, ...]
    baseline: dict[str, _FileContent]
    write_scope: tuple[Path, ...] | None = None

    @classmethod
    def capture(cls, workspace_root: Path, *, application_roots: tuple[Path, ...] | None = None, write_scope: tuple[str, ...] | None = None) -> WorkspaceTurnChanges:
        root = workspace_root.resolve()
        roots = application_state_roots() if application_roots is None else application_roots
        scopes = tuple((root / path).resolve() for path in write_scope) if write_scope is not None else None
        return cls(root, roots, _read_workspace(root, roots, scopes), scopes)

    def unified_diff(self) -> str:
        current = _read_workspace(self.workspace_root, self.application_roots, self.write_scope)
        text_changes = TurnDiffTracker()
        binary_changes: list[str] = []
        for path in sorted(self.baseline.keys() | current.keys()):
            before, after = self.baseline.get(path), current.get(path)
            if before == after:
                continue
            if (before is None or before.text is not None) and (after is None or after.text is not None):
                text_changes.track_change(old_path=path, new_path=path,
                    old_content=before.text if before is not None else None,
                    new_content=after.text if after is not None else None)
                continue
            left = f"a/{path}" if before is not None else "/dev/null"
            right = f"b/{path}" if after is not None else "/dev/null"
            mode = "new file mode 100644\n" if before is None else "deleted file mode 100644\n" if after is None else ""
            binary_changes.append(f"diff --git a/{path} b/{path}\n{mode}index {before.oid if before else ZERO_OID}..{after.oid if after else ZERO_OID}\nBinary files {left} and {right} differ\n")
        return (text_changes.get_unified_diff() or "") + "".join(binary_changes)
