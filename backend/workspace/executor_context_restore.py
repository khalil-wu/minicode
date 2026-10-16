"""Restore recent file excerpts through this compaction's accepted executors."""
from __future__ import annotations

import codecs
from io import IncrementalNewlineDecoder

from backend.agent.environment_selection import bind_tool_execution_environment, close_tool_execution_environment
from backend.workspace.executor_service import ExecutorWorkspaceService, executor_registration_id


async def _text_prefix(service, path, character_limit):
    decoder = IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), translate=True)
    parts, retained, offset = [], 0, 0
    async with await service.filesystem.open(path.uri, sandbox=service.sandbox) as handle:
        while retained <= character_limit:
            chunk, eof = await handle.read_block(offset, min(64 * 1024, character_limit + 1 - retained))
            offset += len(chunk)
            text = decoder.decode(chunk, final=eof)
            parts.append(text)
            retained += len(text)
            if eof:
                break
    return "".join(parts)


async def restored_executor_file_blocks(context, state, *, available_tokens, max_files,
        per_file_tokens, estimate_tokens):
    """Return the existing numbered excerpt format, without changing read guards.

    The live snapshot selects attachments. Read observations only name recent
    files within those attachments; they cannot provision/select a registry ID.
    """
    del state
    snapshot = context.run_context.turn_environments
    attachments = snapshot.turn_environments()
    if not attachments:
        return []
    observed = tuple(context.metadata.get("_executor_observed_files", {}).values())
    output, seen = [], set()
    for attachment in attachments:
        captured = bind_tool_execution_environment(context, attachment)
        try:
            async with ExecutorWorkspaceService(captured) as service:
                registration = executor_registration_id(captured)
                candidates = [item["path_uri"] for item in reversed(observed)
                    if item["environment_id"] == captured.environment_id and item["registration_id"] == registration]
                for value in candidates:
                    path = service.resolve(value)
                    key = registration, path
                    if key in seen or not path.is_relative_to(service.cwd) or not service.scope.resolve_access(path.uri).can_read:
                        continue
                    seen.add(key)
                    metadata = await service.metadata(path)
                    if metadata is None or not metadata["isFile"]:
                        continue
                    maximum = min(available_tokens, per_file_tokens)
                    display = service.display(path)
                    if len(attachments) > 1:
                        display = f"{captured.environment_id}/{display}"
                    prefix, suffix = f"### {display}\n```text\n", "\n```"
                    character_limit = (maximum - estimate_tokens(prefix + suffix)) * 4
                    if character_limit <= 0:
                        return output
                    try:
                        raw = await _text_prefix(service, path, character_limit)
                    except UnicodeDecodeError:
                        continue  # The existing restore surface contains only UTF-8 text excerpts.
                    source_truncated = len(raw) > character_limit
                    numbered = "\n".join(f"{index}: {line}" for index, line in enumerate(raw[:character_limit].splitlines(), 1))
                    if source_truncated or len(numbered) > character_limit:
                        marker = f"... [file view truncated at {maximum:,} tokens] ..."
                        body = numbered[:max(0, character_limit - len(marker) - 1)].rstrip()
                        numbered = f"{body}\n{marker}" if body else marker[:character_limit]
                    block = prefix + numbered + suffix
                    cost = estimate_tokens(block)
                    if cost > available_tokens:
                        return output
                    output.append(block)
                    available_tokens -= cost
                    if len(output) == max_files:
                        return output
        finally:
            await close_tool_execution_environment(captured)
    return output
