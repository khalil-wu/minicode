"""Existing file-tool presentation fed only by the accepted executor's bytes."""
from __future__ import annotations

import base64
import codecs
import re
from pathlib import PurePosixPath
import regex
from pathspec.gitignore import GitIgnoreSpec
from io import IncrementalNewlineDecoder, StringIO
from itertools import islice

from backend.atomic_io import normalize_text_newlines, preserve_text_line_endings
from backend.environments.errors import ExecutorError
from backend.permissions.review import build_structured_diff_payload, generate_unified_diff
from backend.tools.base import ToolResult, truncate_text_head
from backend.tools.file_tools_common import (
    MAX_FILE_READ_BYTES, READ_FILE_MAX_BYTES, READ_FILE_MAX_LINES, _add_line_numbers,
    _generate_limited_unified_diff,
)
from backend.workspace.executor_service import (
    ExecutorWorkspaceService, executor_read_hash, record_executor_file_hash, text_hash, validate_text_hash,
    record_executor_file_observation,
)
from backend.workspace.executor_patch_io import (
    ExecutorChange, emit_executor_change, executor_changes_diff, plan_executor_patch,
)
from backend.tools.apply_patch_parser import ChangeKind


async def _focused_range(service, path, start: int, end: int | None) -> str:
    decoder = IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True)
    output, size, line, offset = [], 0, 1, 0
    async with await service.filesystem.open(path.uri, sandbox=service.sandbox) as handle:
        while True:
            chunk, eof = await handle.read_block(offset)
            offset += len(chunk)
            parts = decoder.decode(chunk, final=eof).split("\n")
            for index, part in enumerate(parts):
                complete = index < len(parts) - 1
                if line >= start and (end is None or line <= end):
                    fragment = part + ("\n" if complete else "")
                    size += len(fragment.encode("utf-8"))
                    if size > MAX_FILE_READ_BYTES:
                        raise ValueError(f"Requested file range exceeds {MAX_FILE_READ_BYTES} bytes")
                    output.append(fragment)
                if complete:
                    line += 1
            if eof or end is not None and line > end:
                return "".join(output)


async def executor_read_file(tool, args: dict, context) -> ToolResult:
    file_path = args.get("file_path") or args.get("path") or ""
    start, end = int(args.get("start_line", 1)), args.get("end_line")
    focused = "start_line" in args or "end_line" in args
    if end is not None and int(end) < start:
        return tool._error_result("end_line must be greater than or equal to start_line")
    try:
        async with ExecutorWorkspaceService(context) as service:
            path = service.resolve(file_path)
            value = await service.metadata(path)
            if value is None:
                return tool._error_result(f"File does not exist: {file_path}")
            if not value["isFile"]:
                return tool._error_result(f"Not a file: {file_path}")
            full_hash = None
            if value["size"] > MAX_FILE_READ_BYTES:
                if not focused or path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    return tool._error_result(f"File is too large ({value['size']} bytes); limit is {MAX_FILE_READ_BYTES} bytes")
                content = await _focused_range(service, path, start, int(end) if end is not None else None)
            else:
                raw = await service.read_bytes(path)
                suffix = path.suffix.lower()
                if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    media_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}[suffix]
                    return ToolResult(content=f"Image {file_path} ({suffix.lstrip('.')}, {len(raw) // 1024}KB).",
                                      images=[{"media_type": media_type, "data": base64.b64encode(raw).decode("ascii")}])
                if suffix == ".pdf":
                    import fitz

                    with fitz.open(stream=raw, filetype="pdf") as document:
                        body = "\n\n".join(f"--- Page {index + 1} ---\n{document[index].get_text('text').rstrip()}"
                                           for index in range(min(len(document), 20)))
                        return tool._success_result(f"PDF {file_path} ({len(document)} page(s), {len(raw) // 1024}KB):\n\n{body}")
                try:
                    full = normalize_text_newlines(raw.decode("utf-8"))
                    full_hash = text_hash(full)
                except UnicodeDecodeError:
                    if not focused:
                        raise
                    full = normalize_text_newlines(raw.decode("utf-8", errors="replace"))
                content = "".join(islice(StringIO(full), start - 1, int(end) if end is not None else None)) if focused else full
            if full_hash is not None:
                record_executor_file_hash(context, path.uri, full_hash)
            else:
                record_executor_file_observation(context, path.uri)
            numbered = _add_line_numbers(content, start_line=start if focused else 1)
            bounded = truncate_text_head(numbered, max_lines=READ_FILE_MAX_LINES, max_bytes=READ_FILE_MAX_BYTES)
            labels = [f"[{'range_hash' if focused else 'content_hash'}: {text_hash(content)}]"]
            if focused:
                labels.append(f"[content_hash: {full_hash}; write-safe full-file version]" if full_hash else
                              "[range only; a write-safe full-file content_hash was unavailable]")
            if not bounded.truncated:
                return tool._success_result(numbered + "\n\n" + "\n".join(labels))
            artifact_id = tool._artifact_store.save(content=content, source=f"read_file({file_path})", type="text")
            preview = bounded.content + f"\n\n[Full content available via read_artifact('{artifact_id}').]"
            return tool._success_result(preview + "\n\n" + "\n".join(labels), artifact_id=artifact_id, artifact_preview=preview)
    except (OSError, ValueError, ExecutorError, ImportError) as error:
        return tool._error_result(f"Failed to read file {file_path}: {error}")


async def _current_text(service, path) -> str | None:
    return None if await service.metadata(path) is None else normalize_text_newlines(await service.read_text(path))


async def executor_write_file(tool, args: dict, context) -> ToolResult:
    try:
        async with ExecutorWorkspaceService(context) as service:
            path = service.resolve(args.get("file_path") or args.get("path") or "")
            content = args["content"]
            async with service.mutate([path]):
                raw = None if await service.metadata(path) is None else await service.read_bytes(path)
                previous = None if raw is None else normalize_text_newlines(raw.decode("utf-8"))
                validate_text_hash(previous, args.get("expected_hash"))
                written = preserve_text_line_endings(content, raw) if raw is not None else content
                await service.write_bytes(path, written.encode("utf-8"))
                record_executor_file_hash(context, path.uri, text_hash(content))
            plan = ExecutorChange(ChangeKind.ADD if previous is None else ChangeKind.UPDATE, path, path, previous, content)
            await emit_executor_change(service, plan)
            _, plus, minus, _ = _generate_limited_unified_diff(previous or "", content, service.display(path), max_chars=0)
            result = tool._success_result(f"Wrote {args.get('file_path', path.uri)} ({len(content.splitlines())} lines, {len(content)} chars). "
                f"content_hash: {text_hash(content)} Diff stats: +{plus} -{minus}.")
            result.runtime_metadata["committed_diff"] = executor_changes_diff(service, [plan])
            return result
    except (OSError, ValueError, ExecutorError) as error:
        return tool._error_result(str(error))


async def executor_edit_file(tool, args: dict, context) -> ToolResult:
    from backend.tools.edit_file import prepare_edit_content

    try:
        async with ExecutorWorkspaceService(context) as service:
            path = service.resolve(args.get("file_path") or args.get("path") or "")
            async with service.mutate([path]):
                raw = await service.read_bytes(path)
                previous = normalize_text_newlines(raw.decode("utf-8"))
                validate_text_hash(previous, args.get("expected_hash"), require=False)
                updated, count = prepare_edit_content(previous, args["old_string"], args["new_string"],
                    file_path=service.display(path), replace_all=args.get("replace_all", False))
                if updated == previous:
                    return tool._success_result(f"No change needed for {service.display(path)}: old_string already equals new_string.")
                await service.write_bytes(path, preserve_text_line_endings(updated, raw).encode("utf-8"))
                record_executor_file_hash(context, path.uri, text_hash(updated))
            plan = ExecutorChange(ChangeKind.UPDATE, path, path, previous, updated)
            await emit_executor_change(service, plan)
            _, plus, minus, _ = _generate_limited_unified_diff(previous, updated, service.display(path), max_chars=0)
            result = tool._success_result(f"Edited {service.display(path)} in {count} occurrence(s). "
                f"Diff stats: +{plus} -{minus}. content_hash: {text_hash(updated)}")
            result.runtime_metadata["committed_diff"] = executor_changes_diff(service, [plan])
            return result
    except (OSError, ValueError, ExecutorError) as error:
        return tool._error_result(str(error))


async def generate_executor_tool_diff(tool_name: str, args: dict, context) -> dict | None:
    """The caller supplies its detached args copy; review never records a read hash."""
    if tool_name not in {"write_file", "edit_file", "apply_patch"}:
        return None
    async with ExecutorWorkspaceService(context) as service:
        if tool_name == "apply_patch":
            plans, expected = await plan_executor_patch(service, args["patch"])
            args["_expected_hashes"] = expected
            return executor_changes_diff(service, plans)
        path = service.resolve(args.get("file_path") or args.get("path") or "")
        previous = await _current_text(service, path)
        # Preserve the actual read-time guard. Reading for approval does not
        # silently bless a file the model has not read or a stale earlier read.
        args["expected_hash"] = executor_read_hash(context, path.uri) or ""
        if tool_name == "write_file":
            updated = args["content"]
        else:
            from backend.tools.edit_file import prepare_edit_content

            if previous is None:
                raise FileNotFoundError(path.uri)
            updated, _ = prepare_edit_content(previous, args["old_string"], args["new_string"],
                file_path=service.display(path), replace_all=args.get("replace_all", False))
        display = service.display(path)
        patch = generate_unified_diff(display, previous or "", updated)
        payload = build_structured_diff_payload(display, patch, status="added" if previous is None else "modified",
                                               size_bytes=len(updated.encode("utf-8")))
        for item in payload.get("files", []):
            item.update(environment_id=service.scope.environment_id, path_uri=path.uri)
        return payload


async def executor_list_files(tool, args, context):
    from backend.tools.file_tools_common import LIST_FILES_MAX_ENTRIES

    try:
        async with ExecutorWorkspaceService(context) as service:
            root = service.resolve(args.get("path") or args.get("directory") or ".")
            limit = int(args.get("limit") or LIST_FILES_MAX_ENTRIES)
            if limit < 1:
                return tool._error_result("limit must be a positive integer")
            pending, output = [root], []
            while pending and len(output) < limit:
                directory = pending.pop()
                for entry in await _directory_entries(service, directory):
                    path = service.resolve(entry["path"])
                    if not service.scope.resolve_access(path.uri).can_read:
                        continue
                    output.append(path.relative_to(root).as_posix() + ("/" if entry["kind"] == "directory" else ""))
                    if len(output) == limit:
                        break
                    if args.get("recursive") and entry["kind"] == "directory":
                        pending.append(path)
            output.sort(key=str.casefold)
            result = "\n".join(output) or "(empty directory)"
            if len(output) == limit:
                result += "\n\n[The requested entry limit was reached.]"
            return tool._success_result(result)
    except (OSError, ValueError, ExecutorError) as error:
        return tool._error_result(str(error))


async def _directory_entries(service, directory):
    # This caller owns recursion; Source depth zero returns only this directory.
    result = await service.walk(directory, max_depth=0)
    if result["errors"]:
        raise OSError(f"Executor filesystem walk failed: {result['errors']}")
    if result["truncated"]:
        raise ValueError("Executor directory exceeds 50000 entries; narrow the path")
    return result["entries"]


async def executor_candidate_files(service, root):
    """Apply existing search ignores to names read from the selected executor."""
    from backend.workspace.fuzzy_search import _IGNORE_DIRS
    from backend.tools.search_support import SearchResourceLimitError

    value = await service.metadata(root)
    if value is None:
        raise FileNotFoundError(root.uri)
    if value["isFile"]:
        if service.scope.resolve_access(root.uri).can_read:
            yield root
        return
    pending, seen = [(root, ())], 0
    while pending:
        directory, specs = pending.pop()
        entries = await _directory_entries(service, directory)
        if any(service.resolve(entry["path"]).name == ".gitignore" and entry["kind"] == "file" for entry in entries):
            ignore = directory.joinpath(".gitignore")
            if service.scope.resolve_access(ignore.uri).can_read:
                specs = (*specs, (directory, GitIgnoreSpec.from_lines((await service.read_text(ignore)).splitlines())))
        for entry in entries:
            seen += 1
            if seen > 50_000:
                raise SearchResourceLimitError("executor search exceeded 50000 entries; narrow the path")
            path = service.resolve(entry["path"])
            is_dir = entry["kind"] == "directory"
            if is_dir and path.name.casefold() in _IGNORE_DIRS or not service.scope.resolve_access(path.uri).can_read:
                continue
            ignored = False
            for base, spec in specs:
                decision = spec.check_file(path.relative_to(base).as_posix() + ("/" if is_dir else "")).include
                if decision is not None:
                    ignored = decision
            if ignored:
                continue
            if is_dir:
                pending.append((path, specs))
            elif entry["kind"] == "file":
                yield path


async def executor_glob_files(tool, args, context):
    from backend.tools.search_support import (_apply_pagination, _bounded_search_output, _coerce_head_limit,
        _coerce_nonnegative_int, _compile_glob_filter, _pagination_suffix, SearchResourceLimitError)
    try:
        async with ExecutorWorkspaceService(context) as service:
            root = service.resolve(args.get("path") or args.get("directory") or ".")
            match = _compile_glob_filter(args["pattern"])
            found = []
            async for path in executor_candidate_files(service, root):
                name = path.relative_to(root).as_posix()
                if match(name):
                    value = await service.metadata(path)
                    found.append((value["modifiedAtMs"], name))
            found.sort(key=lambda item: (-item[0], item[1]))
            limit = _coerce_head_limit(args.get("head_limit"), 100)
            offset = _coerce_nonnegative_int(args.get("offset"), 0)
            page, truncated = _apply_pagination([item[1] for item in found], offset=offset, head_limit=limit)
            result = (f"Found {len(page)} matching files for '{args['pattern']}':\n" + "\n".join(f"- {name}" for name in page)
                if page else f"No files matched the pattern '{args['pattern']}'.")
            return tool._success_result(_bounded_search_output(result + _pagination_suffix(offset=offset, head_limit=limit, truncated=truncated)))
    except (OSError, ValueError, ExecutorError, SearchResourceLimitError) as error:
        return tool._error_result(str(error))


async def executor_grep_files(tool, args, context):
    from backend.tools.command_support import _as_bool
    from backend.tools.search_support import (_apply_pagination, _bounded_search_output, _coerce_head_limit,
        _coerce_nonnegative_int, _compile_glob_filter, _normalize_file_extensions, _normalize_output_mode,
        _pagination_suffix, GREP_MAX_MATCHES, GREP_DISPLAY_LINE_MAX_CHARS, SearchResourceLimitError)

    pattern = args["pattern"]
    flags = regex.IGNORECASE if _as_bool(args.get("-i", args.get("case_insensitive", False))) else 0
    multiline = _as_bool(args.get("multiline", False))
    if multiline:
        flags |= regex.DOTALL | regex.MULTILINE
    try:
        compiled = regex.compile(re.escape(pattern) if _as_bool(args.get("fixed_strings", False)) else pattern, flags)
    except regex.error as error:
        return tool._invalid_regex_result(str(error))
    mode = _normalize_output_mode(args.get("output_mode"))
    file_type = str(args.get("type") or "")
    extensions = _normalize_file_extensions(args.get("file_extensions", []), file_type)
    if file_type and not extensions:
        return tool._error_result(f"File type '{file_type}' requires ripgrep. Use an explicit glob filter for executor filesystem search.")
    glob = _compile_glob_filter(args["glob"]) if args.get("glob") else lambda _name: True
    limit = _coerce_head_limit(args.get("head_limit"), GREP_MAX_MATCHES)
    offset = _coerce_nonnegative_int(args.get("offset"), 0)
    context_lines = _coerce_nonnegative_int(args.get("-C", args.get("context", 0)))
    before = context_lines or _coerce_nonnegative_int(args.get("-B", args.get("before_context", 0)))
    after = context_lines or _coerce_nonnegative_int(args.get("-A", args.get("after_context", 0)))
    numbers = _as_bool(args.get("-n", args.get("line_numbers", True)), default=True)
    found = []
    try:
        async with ExecutorWorkspaceService(context) as service:
            root = service.resolve(args.get("path") or args.get("directory") or ".")
            async for path in executor_candidate_files(service, root):
                name = path.relative_to(root).as_posix() if path != root else path.name
                if not glob(name) or extensions and path.suffix.casefold() not in extensions:
                    continue
                raw = await service.read_bytes(path)
                if b"\0" in raw[:8192]:
                    continue
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines()
                hits = set()
                if multiline:
                    for match in compiled.finditer(text, timeout=0.75):
                        first = text.count("\n", 0, match.start())
                        last = text.count("\n", 0, max(match.start(), match.end() - 1))
                        hits.update(range(first, last + 1))
                else:
                    hits.update(index for index, line in enumerate(lines) if compiled.search(line, timeout=0.75))
                if hits and mode == "files_with_matches":
                    found.append(name)
                elif hits and mode == "count":
                    found.append(f"{name}:{len(hits)}")
                elif hits:
                    shown = set(index for hit in hits for index in range(max(0, hit - before), min(len(lines), hit + after + 1)))
                    found.extend(f"{name}{':' + str(index + 1) if numbers else ''}{':' if index in hits else '-'}{lines[index][:GREP_DISPLAY_LINE_MAX_CHARS]}" for index in sorted(shown))
                if limit is not None and len(found) > offset + limit:
                    break
            page, truncated = _apply_pagination(found, offset=offset, head_limit=limit)
            result = "\n".join(page) or "(no matches)"
            return tool._success_result(_bounded_search_output(result + _pagination_suffix(offset=offset, head_limit=limit, truncated=truncated)))
    except (OSError, ValueError, ExecutorError, SearchResourceLimitError, TimeoutError) as error:
        return tool._error_result(str(error))


async def executor_fuzzy_search(tool, args, context):
    from backend.workspace.fuzzy_search import FuzzySearchEngine
    from backend.tools.search_support import SearchResourceLimitError

    try:
        async with ExecutorWorkspaceService(context) as service:
            # Scoring is pure. Only executor URIs enter the file enumeration.
            scorer = object.__new__(FuzzySearchEngine)
            scorer.workspace_root = PurePosixPath("/")
            matches = []
            async for path in executor_candidate_files(service, service.cwd):
                name = path.relative_to(service.cwd).as_posix()
                pure = PurePosixPath("/") / name
                if not args.get("include_tests", False) and scorer._is_test_file(pure):
                    continue
                match = scorer._score_match(pure, args["query"].lower(), path_str=name)
                if match is not None:
                    matches.append((match.score, name))
            matches.sort(key=lambda item: (-item[0], item[1]))
            output = matches[:args.get("max_results", 20)]
            return tool._success_result("\n".join(f"{index}. {name} (score: {score:.1f})" for index, (score, name) in enumerate(output, 1))
                or f"未找到匹配 '{args['query']}' 的文件")
    except (OSError, ValueError, ExecutorError, SearchResourceLimitError) as error:
        return tool._error_result(str(error))


async def executor_notebook_edit(tool, args, context):
    from backend.atomic_io import preserve_text_line_endings
    from backend.tools.apply_patch_parser import ChangeKind

    try:
        async with ExecutorWorkspaceService(context) as service:
            path = service.resolve(args["notebook_path"])
            if path.suffix.lower() != ".ipynb":
                return tool._error_result(f"Not a notebook (.ipynb): {path.uri}")
            async with service.mutate([path]):
                old = await service.read_text(path)
                validate_text_hash(old, args.get("expected_hash") or executor_read_hash(context, path.uri))
                prepared = tool._edit_content(args, old)
                if isinstance(prepared, ToolResult):
                    return prepared
                new, action, count = prepared
                new = preserve_text_line_endings(new, old.encode("utf-8"))
                await service.write_bytes(path, new.encode("utf-8"))
                record_executor_file_hash(context, path.uri, text_hash(new))
            plan = ExecutorChange(ChangeKind.UPDATE, path, path, old, new)
            await emit_executor_change(service, plan)
            result = tool._success_result(f"{action} in {service.display(path)} ({count} cells). content_hash: {text_hash(new)}")
            result.runtime_metadata["committed_diff"] = executor_changes_diff(service, [plan])
            return result
    except (OSError, ValueError, ExecutorError) as error:
        return tool._error_result(str(error))
