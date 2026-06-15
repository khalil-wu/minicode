from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend.agent.state import AgentState
from backend.agent.harness.search_plan import build_search_plan
from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_sensitive_file
from backend.agent.harness._common import WEB_SEARCH_TOOL_NAMES, WEB_FETCH_TOOL_NAMES, _text_arg

PRIMARY_FILE_METADATA_KEYS = (
    "primaryFile",
    "primary_file",
    "activeFile",
    "active_file",
    "activeTabPath",
    "active_tab_path",
    "currentFile",
    "current_file",
)
WORKSPACE_GROUNDING_IGNORED_NAMES = {
    ".git",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    "artifacts",
}
WORKSPACE_GROUNDING_TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".html",
    ".css",
    ".scss",
    ".sass",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".rs",
    ".go",
    ".java",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
}
_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.I)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}>\u3002\uff0c\uff1b\uff1a\uff01\uff1f"


class ResourceResolver:
    """Resolve implicit user references such as 'my file' or 'that URL'."""

    def __init__(
        self,
        state: AgentState,
        tool_ctx: ToolExecutionContext | None = None,
        *,
        reserved_fetch_urls: set[str] | None = None,
    ) -> None:
        self.state = state
        self.tool_ctx = tool_ctx
        self.reserved_fetch_urls = reserved_fetch_urls

    def resolve(self, role: str) -> Any:
        if role == "workspace_file":
            return (
                inferred_read_file_path_from_recent_list(self.state)
                or inferred_read_file_path_from_workspace(self.state, self.tool_ctx)
            )
        if role == "workspace_output_path":
            return inferred_output_path_from_intent(self.state)
        if role == "search_query":
            return build_search_plan(self.state.user_message.strip()).normalized_query
        if role == "latest_url":
            candidates = web_fetch_candidate_urls(self.state, exclude=self.reserved_fetch_urls)
            return candidates[0] if candidates else ""
        if role == "latest_artifact":
            return inferred_artifact_id_from_state(self.state)
        return ""


def clean_candidate_url(value: str) -> str:
    url = str(value or "").strip().strip("<>")
    return url.rstrip(_URL_TRAILING_PUNCTUATION)


def _extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(str(text or "")):
        url = clean_candidate_url(match.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _used_web_fetch_urls(state: AgentState) -> set[str]:
    used: set[str] = set()
    for record in state.tool_calls:
        if record.tool_name not in WEB_FETCH_TOOL_NAMES:
            continue
        url = _text_arg(record.tool_input.get("url"))
        if url:
            used.add(clean_candidate_url(url))
    return used


def web_fetch_candidate_urls(
    state: AgentState,
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    """Return unfetched URLs from previous search observations, newest search first."""
    blocked = set(exclude or set()) | _used_web_fetch_urls(state)
    urls: list[str] = []
    seen: set[str] = set(blocked)
    for record in reversed(state.tool_calls):
        if record.tool_name not in WEB_SEARCH_TOOL_NAMES:
            continue
        if record.status != "success":
            continue
        for url in _extract_urls_from_text(record.tool_output or ""):
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def _listed_files_from_tool_output(output: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or line.startswith("---"):
            continue
        if line.endswith("/"):
            continue
        match = re.match(r"(.+?)\s+\([0-9][^)]*\)$", line)
        if not match:
            continue
        candidate = match.group(1).strip()
        if not candidate or candidate.endswith("/"):
            continue
        normalized = candidate.replace("\\", "/")
        if normalized not in seen:
            files.append(normalized)
            seen.add(normalized)
    return files


def inferred_read_file_path_from_recent_list(state: AgentState) -> str:
    for record in reversed(state.tool_calls):
        if record.tool_name != "list_files" or record.status != "success":
            continue
        files = _listed_files_from_tool_output(record.tool_output or "")
        if len(files) == 1:
            return files[0]
        return ""
    return ""


def _workspace_root_for_repair(state: AgentState, tool_ctx: ToolExecutionContext | None = None) -> Path | None:
    candidates = [
        getattr(tool_ctx, "workspace_root", None) if tool_ctx else None,
        getattr(getattr(state, "workspace_context", None), "root_path", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            root = Path(candidate).resolve()
        except OSError:
            continue
        if root.exists() and root.is_dir():
            return root
    return None


def _workspace_relative_readable_file(root: Path, path_value: Any) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    try:
        path = Path(raw)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return ""
    if not _is_workspace_grounding_candidate(resolved):
        return ""
    return resolved.relative_to(root).as_posix()


def _path_like_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("path", "file_path", "filePath", "relative_path", "relativePath"):
            candidate = value.get(key)
            if str(candidate or "").strip():
                return candidate
        return ""
    return value


def _metadata_path_values(source: Any) -> list[Any]:
    if source is None:
        return []
    values: list[Any] = []
    if isinstance(source, dict):
        for key in PRIMARY_FILE_METADATA_KEYS:
            if key in source:
                values.append(_path_like_value(source.get(key)))
        for nested_key in ("workspace", "editor", "context"):
            nested = source.get(nested_key)
            if isinstance(nested, dict):
                values.extend(_metadata_path_values(nested))
        return values
    for key in PRIMARY_FILE_METADATA_KEYS:
        if hasattr(source, key):
            values.append(_path_like_value(getattr(source, key)))
    return values


def _inferred_read_file_path_from_primary_metadata(
    state: AgentState,
    root: Path,
    tool_ctx: ToolExecutionContext | None = None,
) -> str:
    metadata = getattr(tool_ctx, "metadata", None) if tool_ctx else None
    sources = [
        metadata,
        getattr(state, "workspace_context", None),
        metadata.get("workspace_context") if isinstance(metadata, dict) else None,
    ]
    for source in sources:
        for value in _metadata_path_values(source):
            inferred = _workspace_relative_readable_file(root, value)
            if inferred:
                return inferred
    return ""


def _is_workspace_grounding_candidate(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if any(part in WORKSPACE_GROUNDING_IGNORED_NAMES for part in path.parts):
        return False
    if path.name.startswith("."):
        return False
    if is_sensitive_file(path):
        return False
    return path.suffix.lower() in WORKSPACE_GROUNDING_TEXT_SUFFIXES


def _single_indexed_workspace_file(state: AgentState, root: Path) -> str:
    workspace_context = getattr(state, "workspace_context", None)
    file_index = getattr(workspace_context, "file_index", None)
    if not isinstance(file_index, dict) or not file_index:
        return ""
    candidates: list[str] = []
    for rel_path, entry in file_index.items():
        if getattr(entry, "is_text", True) is False:
            continue
        inferred = _workspace_relative_readable_file(root, rel_path)
        if inferred:
            candidates.append(inferred)
        if len(candidates) > 1:
            return ""
    return candidates[0] if len(candidates) == 1 else ""


def _single_top_level_workspace_file(root: Path) -> str:
    candidates: list[str] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return ""
    for entry in entries:
        if entry.name in WORKSPACE_GROUNDING_IGNORED_NAMES:
            continue
        if entry.is_dir():
            continue
        if not _is_workspace_grounding_candidate(entry):
            continue
        candidates.append(entry.relative_to(root).as_posix())
        if len(candidates) > 1:
            return ""
    return candidates[0] if len(candidates) == 1 else ""


def inferred_read_file_path_from_workspace(
    state: AgentState,
    tool_ctx: ToolExecutionContext | None = None,
) -> str:
    root = _workspace_root_for_repair(state, tool_ctx)
    if root is None:
        return ""
    primary_path = _inferred_read_file_path_from_primary_metadata(state, root, tool_ctx)
    if primary_path:
        return primary_path
    indexed_path = _single_indexed_workspace_file(state, root)
    if indexed_path:
        return indexed_path
    return _single_top_level_workspace_file(root)


def inferred_output_path_from_intent(state: AgentState) -> str:
    message = str(getattr(state, "user_message", "") or "")
    return _explicit_workspace_output_path(message)


def _explicit_workspace_output_path(text: str) -> str:
    candidates = re.findall(
        r"(?i)(?:write|create|生成|写入|保存|更新)\s+(?:file\s+)?[`'\"]?([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_+-]+)[`'\"]?",
        text,
    )
    for candidate in candidates:
        value = candidate.strip().replace("\\", "/")
        if value and "://" not in value and not value.startswith("../"):
            return value
    inline = re.findall(r"`([^`]+\.[A-Za-z0-9_+-]+)`", text)
    for candidate in inline:
        value = candidate.strip().replace("\\", "/")
        if value and "://" not in value and not value.startswith("../"):
            return value
    return ""


def inferred_artifact_id_from_state(state: AgentState) -> str:
    for artifact_id in reversed(state.artifact_refs):
        text = str(artifact_id or "").strip()
        if text:
            return text
    for record in reversed(state.tool_calls):
        text = str(getattr(record, "artifact_id", "") or "").strip()
        if text:
            return text
    return ""
