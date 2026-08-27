"""Subagent orchestration helpers shared by TaskTool and control-plane tools.

Extracted from ``backend/tools/agent_tools.py`` so the pure helper layer
(subagent LLM resolution, scope narrowing, metadata normalization, hooks) is
independent of the tool class that consumes it.
"""

from __future__ import annotations

import logging

from backend.agent.context import ContextBuilder
from backend.agent.prompt_cache import prompt_cache_fork_diagnostic
from backend.agents.loader import (
    discover_agents,
    get_custom_agent,
)
from backend.artifact.store import ArtifactStore
from backend.config import (
    AppConfig,
    load_config,
)
from backend.llm.model_selection import (
    REASONING_LEVEL_ORDER,
    apply_model_thinking_level,
    config_with_model_budget,
    default_model_thinking_level,
    model_thinking_levels,
)
from backend.tools.agent_control_plane import (
    normalize_agent_fork_turns,
    normalize_agent_task_name,
)
from backend.tools.base import MAX_TOOL_RESULT_BYTES
from backend.tools.subagent_catalog import (
    BUILTIN_AGENT_TYPES,
    available_agent_types,
)
from backend.tools.subagent_result import compact_subagent_result
from backend.tools.toolset_runtime import restore_toolset_policy
from backend.tools.toolsets import (
    ACTIVE_TOOLSET_POLICY_METADATA_KEY,
    SESSION_TOOLSET_POLICY_METADATA_KEY,
)
from contextlib import suppress
from dataclasses import (
    dataclass,
    replace,
)
from pathlib import (
    Path,
    PurePosixPath,
)
from typing import Any
import asyncio
import inspect
import os
import re


logger = logging.getLogger(__name__)


_SUBAGENT_CAPACITY_MESSAGE = "Maximum concurrent subagents reached. Wait for a running task to finish."
# Keep request and execution limits separate so a single call can queue work
# instead of silently dropping tasks.
MAX_PARALLEL_TASKS = 8
MAX_PARALLEL_CONCURRENCY = 4
_MODEL_FAMILY_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable"})
# Keep the subagent transport on the same inline contract as every other tool.
# The result pipeline owns persistence/truncation; this helper only provides a
# recoverable artifact for direct TaskTool callers that bypass that pipeline.
_SUBAGENT_RESULT_ARTIFACT_THRESHOLD_BYTES = MAX_TOOL_RESULT_BYTES

_PUBLIC_PERMISSION_MODE_TO_INTERNAL = {
    "confirm": "confirm",
    "auto": "auto",
    "bypass": "bypass",
    "plan": "plan",
}


def _sanitize_teammate_name(name: str) -> str:
    """Keep the public teammate identity unambiguous in mailbox keys."""

    return str(name or "").replace("@", "-").strip()


def _xml_attribute(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_teammate_message(
    sender: str,
    content: str,
    *,
    summary: str = "",
) -> str:
    """Build the stable in-process teammate prompt envelope."""

    summary_attr = (
        f' summary="{_xml_attribute(summary)}"' if str(summary or "").strip() else ""
    )
    return (
        f'<teammate-message teammate_id="{_xml_attribute(sender)}"{summary_attr}>\n'
        f"{str(content or '')}\n"
        "</teammate-message>"
    )

def _externalize_large_subagent_result(
    artifact_store: ArtifactStore,
    *,
    subagent_id: str,
    content: str,
) -> tuple[str, str]:
    """Keep large delegated reports out of the parent model context."""

    text = str(content or "")
    if len(text.encode("utf-8")) <= _SUBAGENT_RESULT_ARTIFACT_THRESHOLD_BYTES:
        return text, ""
    try:
        artifact_id = artifact_store.save(
            content=text,
            source=f"subagent:{subagent_id}",
            type="subagent_result",
        )
    except Exception as exc:
        logger.warning("large subagent result artifact save failed id=%s: %s", subagent_id, exc)
        return text, ""
    compact, _ = compact_subagent_result(text)
    return (
        "\n".join(
            (
                compact,
                "",
                "Full delegated result stored as artifact.",
                f"artifact_id: {artifact_id}",
                f"original_chars: {len(text)}",
                "Use read_artifact with this artifact_id only if the retained summary is insufficient.",
            )
        ).strip(),
        artifact_id,
    )


def _custom_agent_deny_rules(
    agent_type: str,
    tool_registry: Any | None,
    workspace_root: str | Path | None = None,
) -> list[str]:
    """Enforce a custom agent's tool restrictions as deny rules.

    A custom AgentDefinition may declare a ``tools`` whitelist and/or
    ``disallowed_tools``. Without this they were loaded and saved but never
    applied — so a user who set ``disallowed_tools: [write_file]`` would still
    see the subagent write. Returns [] for built-in agent types or unrestricted
    custom agents.
    """
    if agent_type in BUILTIN_AGENT_TYPES:
        return []
    try:
        custom = get_custom_agent(agent_type, workspace_root)
    except Exception as exc:
        raise RuntimeError(
            f"Custom agent definition '{agent_type}' could not be loaded; "
            "the child was not started."
        ) from exc
    if custom is None:
        return []
    deny: list[str] = []
    deny.extend(str(t).strip() for t in (custom.disallowed_tools or []) if str(t).strip())
    whitelist = [str(t).strip() for t in (custom.tools or []) if str(t).strip()]
    # "*" means all tools; it is not a literal tool name.
    if "*" in whitelist:
        whitelist = []
    if whitelist:
        allowed = set(whitelist)
        try:
            if tool_registry is None:
                raise RuntimeError("tool registry is unavailable")
            all_names = tool_registry.list_tools()
        except Exception as exc:
            raise RuntimeError(
                f"Custom agent '{agent_type}' tool surface could not be inspected; "
                "the child was not started."
            ) from exc
        deny.extend(t for t in all_names if t not in allowed)
    seen: set[str] = set()
    unique: list[str] = []
    for name in deny:
        if name and name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _user_visible_progress_text(value: Any) -> str:
    return str(value or "").strip()


def _subagent_display_summary(value: Any) -> str:
    """Extract one plain, stable summary line from a child result."""
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or re.match(r"^#{1,6}\s+", line):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"(?:\*\*|__|`)", "", line).strip()
        if not line or re.match(r"^Subagent\s+subagent-[\w-]+.*completed", line, re.IGNORECASE):
            continue
        return line[:500]
    return ""


def _prompt_scope_summary(prompt: str) -> str:
    """Compact user-facing scope label derived from the task prompt."""
    text = " ".join(str(prompt or "").split())
    return text if len(text) <= 120 else f"{text[:80]} … {text[-39:]}"


def _exclusive_parallel_task_scopes(tasks: list[dict[str, Any]]) -> list[str]:
    """Return explicit scope labels after checking structured write ownership.

    Natural-language similarity is deliberately irrelevant here: two workers
    may independently review the same problem.  Only overlapping write_scope
    paths are mechanically unsafe to schedule in parallel.
    """
    # Enforce write_scope exclusivity between sibling workers. Two parallel
    # workers whose write_scope shares a path would race on that file
    # (last-writer-wins) with no mutual exclusion, so reject the batch — the
    # caller surfaces this as "non-overlapping assignment" guidance.
    seen_write_paths: list[str] = []
    for task in tasks:
        # Read-only/exploration workers do not mutate the workspace and should
        # not create artificial overlap conflicts with write-capable workers.
        if bool(task.get("read_only")) or str(task.get("agent_type") or "").lower() in {"explore", "plan"}:
            continue
        raw_scope = task.get("write_scope")
        paths = raw_scope if isinstance(raw_scope, list) else []
        for path in paths:
            norm = os.path.normcase(
                os.path.normpath(str(path or "").strip())
            ).strip("/\\")
            if not norm:
                continue
            if any(
                norm == existing
                or norm.startswith(f"{existing}{os.sep}")
                or existing.startswith(f"{norm}{os.sep}")
                for existing in seen_write_paths
            ):
                return []
            seen_write_paths.append(norm)

    scopes: list[str] = []
    for index, task in enumerate(tasks, 1):
        scope = str(
            task.get("description")
            or task.get("objective")
            or _prompt_scope_summary(str(task.get("prompt") or ""))
            or f"parallel task {index}"
        ).strip()
        scopes.append(scope)
    return scopes


def _parallel_undeclared_writers(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return write-capable parallel tasks that declare no write_scope.

    Two writers with no disjoint write_scope race on the same file
    (last-writer-wins) with no mutual exclusion, so they must not be scheduled
    concurrently. Read-only tasks (explore/plan or explicit read_only) may
    overlap freely — independent review of the same files is safe.
    """
    writers: list[dict[str, Any]] = []
    for task in tasks:
        if bool(task.get("read_only")):
            continue
        if str(task.get("agent_type") or "").lower() in {"explore", "plan"}:
            continue
        raw_scope = task.get("write_scope")
        paths = raw_scope if isinstance(raw_scope, list) else []
        declared = any(
            str(path or "").strip().replace("\\", "/").strip("/")
            for path in paths
        )
        if not declared:
            writers.append(task)
    return writers


def _available_agent_types() -> list[str]:
    """Return built-in plus discovered custom subagent types for model schema."""
    return available_agent_types(discover_agents)


@dataclass(frozen=True)
class _SubagentLLMResolution:
    """Concrete child model config copied from the live parent turn."""

    llm: Any
    config: AppConfig
    provider: str
    model: str
    effort: str
    owns_llm: bool = False


async def _close_subagent_llm(adapter: Any) -> None:
    """Close one child-owned adapter without ever touching the parent adapter."""

    close = getattr(adapter, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _close_subagent_llm_resolution(
    resolution: _SubagentLLMResolution | None,
) -> None:
    if resolution is None or not resolution.owns_llm:
        return
    await _close_subagent_llm(resolution.llm)


def _primary_llm_adapter(adapter: Any) -> Any:
    return adapter


def _adapter_provider_model(adapter: Any) -> tuple[str, str]:
    primary = _primary_llm_adapter(adapter)
    settings = getattr(primary, "_settings", None)
    provider = str(
        getattr(settings, "provider", "")
        or getattr(primary, "_provider", "")
        or getattr(primary, "_provider_id", "")
        or ""
    ).strip()
    model = str(
        getattr(settings, "model", "")
        or getattr(getattr(primary, "_model", None), "id", "")
        or getattr(primary, "_model", "")
        or ""
    ).strip()
    return provider, model


def _configured_subagent_overrides(
    *,
    agent_type: str,
    model_override: str,
    effort_override: str,
    workspace_root: str | Path | None,
) -> tuple[str, str, bool, bool]:
    """Resolve canonical precedence: task arguments override agent defaults."""

    custom = get_custom_agent(agent_type, workspace_root) if agent_type else None
    explicit_model = bool(str(model_override or "").strip())
    explicit_effort = bool(str(effort_override or "").strip())
    model = str(model_override or "").strip()
    effort = str(effort_override or "").strip()
    if not explicit_model and custom is not None:
        model = str(getattr(custom, "model", "") or "").strip()
    if not explicit_effort and custom is not None:
        effort = str(getattr(custom, "effort", "") or "").strip()
    return model, effort, explicit_model, explicit_effort


async def _resolve_subagent_llm(
    inherited_llm: Any,
    *,
    parent_metadata: dict[str, Any] | None,
    agent_type: str,
    model_override: str = "",
    effort_override: str = "",
    workspace_root: str | Path | None = None,
    build_adapter: bool = True,
) -> _SubagentLLMResolution:
    """Resolve one child using the live parent config and ModelRuntime.

    Resolve the child from the live turn config. Explicit task overrides take
    precedence, while omitted model/thinking values inherit the parent.
    """

    metadata = parent_metadata if isinstance(parent_metadata, dict) else {}
    snapshot = metadata.get("_subagent_parent_runtime")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    inherited_llm = snapshot.get("llm") or inherited_llm
    if inherited_llm is None:
        raise ValueError("Subagent runtime has no parent LLM to inherit.")

    parent_config = snapshot.get("config")
    if not (
        isinstance(parent_config, AppConfig)
        or (
            parent_config is not None
            and hasattr(parent_config, "agent")
            and hasattr(parent_config, "token_budget")
        )
    ):
        try:
            parent_config = load_config(cwd=workspace_root)
        except TypeError:
            parent_config = load_config()

    inferred_provider, inferred_model = _adapter_provider_model(inherited_llm)
    parent_provider = str(snapshot.get("provider") or inferred_provider).strip()
    parent_model = str(snapshot.get("model") or inferred_model).strip()
    model_runtime = snapshot.get("model_runtime")
    parent_effort = str(snapshot.get("thinking_level") or "").strip().lower()
    if not parent_effort:
        primary = _primary_llm_adapter(inherited_llm)
        current_effort = getattr(primary, "current_reasoning_effort", None)
        parent_effort = str(
            (current_effort() if callable(current_effort) else "")
            or getattr(getattr(primary, "_settings", None), "reasoning_effort", "")
            or getattr(getattr(parent_config, "llm", None), "reasoning_effort", "")
            or "off"
        ).strip().lower()

    requested_model, requested_effort, _explicit_model, _explicit_effort = (
        _configured_subagent_overrides(
            agent_type=agent_type,
            model_override=model_override,
            effort_override=effort_override,
            workspace_root=workspace_root,
        )
    )
    # An explicitly selected model owns its own default reasoning level, even
    # when it happens to equal the parent's current model.  Only an omitted
    # (or explicit ``inherit``) model inherits the parent's effort.  This is
    # Comparing only the final model IDs would silently leak the parent's
    # effort, so explicit selection stays distinct from inheritance.
    model_inherits = requested_model.lower() in {"", "inherit"}
    effort_inherits = requested_effort.lower() in {"", "inherit"}
    model_was_selected = not model_inherits
    available_snapshot = tuple(snapshot.get("available_models") or ())

    target_provider = parent_provider
    target_model = parent_model
    if not model_inherits:
        target_model = requested_model
        alias = requested_model.strip().lower()
        if alias in _MODEL_FAMILY_ALIASES:
            # Keep the parent's exact model when a bare family alias already
            # matches; otherwise resolve it within the current provider before
            # applying provider-qualified model selection.
            if alias in parent_model.lower():
                target_model = parent_model
            elif model_runtime is not None:
                alias_match = next(
                    (
                        candidate
                        for candidate in model_runtime.get_models(parent_provider)
                        if alias in candidate.id.lower()
                        or alias in str(getattr(candidate, "name", "") or "").lower()
                    ),
                    None,
                )
                if alias_match is not None:
                    target_model = alias_match.id
            else:
                alias_match = next(
                    (
                        str(candidate).strip()
                        for candidate in available_snapshot
                        if alias in str(candidate).lower()
                    ),
                    "",
                )
                if alias_match:
                    target_model = alias_match
        parent_exact_model = (
            model_runtime.get_model(parent_provider, target_model)
            if model_runtime is not None and parent_provider
            else None
        )
        if "/" in target_model and model_runtime is not None and parent_exact_model is None:
            provider_prefix, qualified_model = target_model.split("/", 1)
            resolved_prefix = provider_prefix
            provider_definition = model_runtime.get_provider(resolved_prefix)
            if provider_definition is None and provider_prefix.lower() in {
                "openai",
                "anthropic",
                "custom",
            }:
                resolved_prefix = provider_prefix.lower()
                provider_definition = model_runtime.get_provider(resolved_prefix)
            if provider_definition is not None and qualified_model:
                target_provider = resolved_prefix
                target_model = qualified_model

    if not target_provider or not target_model:
        if model_inherits and effort_inherits:
            return _SubagentLLMResolution(
                llm=inherited_llm,
                config=parent_config,
                provider=parent_provider,
                model=parent_model,
                effort=parent_effort,
                owns_llm=False,
            )
        raise ValueError("Unable to resolve the child provider/model from the parent turn.")

    runtime_model = None
    if model_runtime is not None:
        runtime_model = model_runtime.get_model(target_provider, target_model)
        if runtime_model is None:
            available = [model.id for model in model_runtime.get_models(target_provider)]
            suffix = f" Available models: {', '.join(available[:20])}." if available else ""
            raise ValueError(
                f"Unknown model '{target_provider}/{target_model}' for subagent.{suffix}"
            )
    else:
        available = available_snapshot
        if (
            not model_inherits
            and target_provider == parent_provider
            and available
            and target_model not in available
        ):
            raise ValueError(
                f"Unknown model '{target_model}' for subagent. "
                f"Available models: {', '.join(str(item) for item in available[:20])}."
            )

    model_changed = target_provider != parent_provider or target_model != parent_model
    available_efforts = model_thinking_levels(
        runtime_model,
        inherited_llm if not model_changed else None,
    )
    if (
        not available_efforts
        and runtime_model is not None
        and bool(getattr(runtime_model, "reasoning", False))
        and model_runtime is not None
        and model_runtime.get_registered_provider_config(target_provider) is not None
    ):
        available_efforts = REASONING_LEVEL_ORDER

    if not effort_inherits:
        effective_effort = requested_effort.strip().lower()
        if effective_effort not in available_efforts:
            supported = ", ".join(available_efforts) or "none"
            raise ValueError(
                f"Reasoning effort '{effective_effort}' is not supported for model "
                f"'{target_provider}/{target_model}'. Supported reasoning efforts: {supported}."
            )
    elif model_changed or model_was_selected:
        effective_effort = default_model_thinking_level(runtime_model, available_efforts)
    else:
        effective_effort = parent_effort

    child_config = config_with_model_budget(
        parent_config,
        model_runtime=model_runtime,
        provider=target_provider,
        model=target_model,
    )
    child_llm_settings = getattr(child_config, "llm", None)
    if child_llm_settings is not None and (
        str(getattr(child_llm_settings, "provider", "") or "") != target_provider
        or str(getattr(child_llm_settings, "model", "") or "") != target_model
        or str(getattr(child_llm_settings, "reasoning_effort", "") or "").strip().lower()
        != effective_effort
    ):
        with suppress(TypeError, ValueError):
            child_config = replace(
                child_config,
                llm=replace(
                    child_llm_settings,
                    provider=target_provider,
                    model=target_model,
                    reasoning_effort=effective_effort,
                ),
            )
    requires_fresh_adapter = model_changed or not effort_inherits
    if not requires_fresh_adapter or not build_adapter:
        return _SubagentLLMResolution(
            llm=inherited_llm,
            config=child_config,
            provider=target_provider,
            model=target_model,
            effort=effective_effort,
            owns_llm=False,
        )

    from backend.llm.model_registry import create_session_llm

    adapter = None
    try:
        # Provider/model resolution may execute configured command-backed
        # values and construct a native adapter. Keep that synchronous work
        # off the shared asyncio loop before publishing the child turn.
        adapter = await asyncio.to_thread(
            create_session_llm,
            child_config,
            model_override=target_model,
            provider_override=target_provider,
            model_runtime=model_runtime,
        )
        if adapter is None:
            raise RuntimeError(
                f"Unable to create subagent adapter for '{target_provider}/{target_model}'."
            )
        built_efforts = model_thinking_levels(runtime_model, adapter)
        if not effort_inherits and effective_effort not in built_efforts:
            supported = ", ".join(built_efforts) or "none"
            raise ValueError(
                f"Reasoning effort '{effective_effort}' is not supported for model "
                f"'{target_provider}/{target_model}'. Supported reasoning efforts: {supported}."
            )
        if effort_inherits and model_changed:
            effective_effort = default_model_thinking_level(runtime_model, built_efforts)
        apply_model_thinking_level(
            adapter,
            runtime_model,
            effective_effort or "off",
        )
        # Keep the child config snapshot and concrete adapter in lock step. A
        # model switch must not leave extensions or resume metadata observing
        # the parent's provider/model settings.
        primary_adapter = _primary_llm_adapter(adapter)
        adapter_settings = getattr(primary_adapter, "_settings", None)
        if adapter_settings is not None:
            with suppress(TypeError, ValueError):
                child_config = replace(child_config, llm=adapter_settings)
        elif getattr(child_config, "llm", None) is not None:
            with suppress(TypeError, ValueError):
                child_config = replace(
                    child_config,
                    llm=replace(
                        child_config.llm,
                        api_key=str(
                            getattr(primary_adapter, "_api_key", "")
                            or getattr(child_config.llm, "api_key", "")
                        ),
                        provider=str(
                            getattr(primary_adapter, "_provider_id", "")
                            or target_provider
                        ),
                        base_url=str(
                            getattr(primary_adapter, "_base_url", "")
                            or getattr(child_config.llm, "base_url", "")
                        ),
                        model=str(
                            getattr(primary_adapter, "_model", "")
                            or target_model
                        ),
                        reasoning_effort=effective_effort,
                        wire_api=str(
                            getattr(
                                getattr(primary_adapter, "capabilities", None),
                                "wire_api",
                                "",
                            )
                            or getattr(child_config.llm, "wire_api", "chat")
                        ),
                    ),
                )
    except BaseException:
        if adapter is not None:
            with suppress(Exception):
                await _close_subagent_llm(adapter)
        raise
    return _SubagentLLMResolution(
        llm=adapter,
        config=child_config,
        provider=target_provider,
        model=target_model,
        effort=effective_effort,
        owns_llm=True,
    )


def _string_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _bool_field(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return bool(raw)


def _subagent_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    raw_effort = (
        raw.get("reasoning_effort")
        if "reasoning_effort" in raw
        else raw.get("effort")
    )
    has_detach = "detach_from_parent" in raw
    has_cancel = "cancel_with_parent" in raw
    if has_detach:
        detach_from_parent = _bool_field(raw.get("detach_from_parent"), False)
        cancel_with_parent = (
            _bool_field(raw.get("cancel_with_parent"), not detach_from_parent)
            if has_cancel
            else (not detach_from_parent)
        )
    elif has_cancel:
        cancel_with_parent = _bool_field(raw.get("cancel_with_parent"), True)
        detach_from_parent = not cancel_with_parent
    else:
        cancel_with_parent = True
        detach_from_parent = False
    if detach_from_parent:
        cancel_with_parent = False
    teammate_name = str(
        raw.get("name")
        or raw.get("teammate_name")
        or ""
    ).strip()
    team_name = str(raw.get("team_name") or "").strip()
    public_mode = str(raw.get("mode") or "").strip()
    internal_mode = _PUBLIC_PERMISSION_MODE_TO_INTERNAL.get(
        public_mode,
        public_mode if public_mode in _PUBLIC_PERMISSION_MODE_TO_INTERNAL.values() else "",
    )
    team_mode = bool(team_name and teammate_name)
    if not team_mode and _bool_field(raw.get("team_mode") or raw.get("is_teammate"), False):
        # Durable transcripts from an already-running teammate retain the
        # derived runtime bit, but new model calls cannot set it directly.
        team_mode = True
    plan_mode_required = team_mode and (
        internal_mode == "plan" or _bool_field(raw.get("plan_mode_required"), False)
    )
    return {
        "cancel_with_parent": cancel_with_parent,
        "detach_from_parent": detach_from_parent,
        "read_only": _bool_field(raw.get("read_only"), False),
        "write_scope": _string_list(raw.get("write_scope")),
        # LLM overrides let a caller pin a model/reasoning effort instead of
        # inheriting the session.
        "provider": str(raw.get("provider") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "effort": str(raw_effort or "").strip(),
        # Worktree isolation runs the subagent inside a temporary git worktree
        # instead of the shared workspace.
        "isolation": str(raw.get("isolation") or "").strip().lower(),
        "cwd": str(raw.get("cwd") or "").strip(),
        # Public teammate fields are {name, team_name, mode}; the booleans
        # below are derived runtime ownership metadata.
        "team_mode": team_mode,
        "plan_mode_required": plan_mode_required,
        "mode": "plan" if plan_mode_required else internal_mode,
        "team_name": team_name,
        "team_id": str(raw.get("team_id") or "").strip(),
        "teammate_name": teammate_name,
        "teammate_id": str(raw.get("teammate_id") or "").strip(),
        "plan_slug": str(raw.get("plan_slug") or "").strip(),
    }


def _nonempty_subagent_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    metadata = _subagent_metadata(raw)
    result = {
        key: value
        for key, value in metadata.items()
        if key != "effort" and value not in ("", [], None)
    }
    if metadata.get("effort") not in ("", None):
        result["reasoning_effort"] = metadata["effort"]
    return result


def _scope_is_within_any(scope: str, ceiling: list[str]) -> bool:
    """True when one workspace-relative scope sits inside any ceiling scope."""
    candidate = PurePosixPath(str(scope).replace("\\", "/")).as_posix().strip("/")
    for allowed in ceiling:
        root = PurePosixPath(str(allowed).replace("\\", "/")).as_posix().strip("/")
        if not root or candidate == root or candidate.startswith(f"{root}/"):
            return True
    return False


def _narrowed_subagent_scope_metadata(
    inherited: dict[str, Any],
    requested: dict[str, Any],
) -> dict[str, Any]:
    """Merge a child's requested fence into the inherited one by narrowing.

    ``read_only`` and ``write_scope`` are the keys ``subagent_scope_guard_reason``
    enforces, and both are model-facing on the ``task`` tool. Spreading the
    child's request last let it widen the parent's fence — and because the
    filter keeps ``False``, an unset ``read_only`` always cleared an inherited
    ``read_only: True``. Delegation may narrow authority, never widen it.
    """
    narrowed = dict(requested)
    narrowed["read_only"] = bool(inherited.get("read_only")) or bool(
        requested.get("read_only")
    )
    parent_scope = [
        str(scope) for scope in (inherited.get("write_scope") or []) if str(scope).strip()
    ]
    child_scope = [
        str(scope) for scope in (requested.get("write_scope") or []) if str(scope).strip()
    ]
    if parent_scope:
        kept = [scope for scope in child_scope if _scope_is_within_any(scope, parent_scope)]
        narrowed["write_scope"] = kept or parent_scope
    elif child_scope:
        narrowed["write_scope"] = child_scope
    else:
        narrowed.pop("write_scope", None)
    return narrowed


def _persisted_session_toolset_policy(
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Project the parent-owned capability ceiling into durable child config."""

    if SESSION_TOOLSET_POLICY_METADATA_KEY in metadata:
        raw = metadata[SESSION_TOOLSET_POLICY_METADATA_KEY]
    elif ACTIVE_TOOLSET_POLICY_METADATA_KEY in metadata:
        # Older sessions had only the effective policy.  It is a safe, narrower
        # fallback for their first resume; new sessions always write the
        # dedicated session key and therefore do not freeze per-turn state.
        raw = metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY]
    else:
        return None
    try:
        return restore_toolset_policy(
            raw,
            label="parent session tool capability policy",
        ).to_mapping()
    except ValueError as exc:
        raise RuntimeError("parent session tool capability policy is invalid") from exc


def _normalize_child_task_name(raw: Any) -> str:
    return normalize_agent_task_name(raw, required=False)


def _normalize_fork_turns(raw: Any) -> str:
    return normalize_agent_fork_turns(raw, default="none")


def _fork_snapshot_for_child(
    parent_metadata: dict[str, Any],
    fork_turns: str,
) -> dict[str, Any] | None:
    mode = _normalize_fork_turns(fork_turns)
    if mode == "none":
        return None
    parent_builder = parent_metadata.get("_context_builder")
    export_snapshot = getattr(parent_builder, "export_snapshot", None)
    if not callable(export_snapshot):
        raise ValueError(
            "fork_turns requires an active parent ContextBuilder"
        )
    snapshot = export_snapshot()
    history = snapshot.get("history") if isinstance(snapshot, dict) else None
    if not isinstance(history, list):
        raise ValueError("parent context snapshot history is unavailable")

    # Keep the current user input but drop the assistant message that contains
    # the in-flight spawn call and any later tool protocol items. This avoids
    # forking a dangling tool-call/result pair into the child.
    user_indices = [
        index
        for index, item in enumerate(history)
        if isinstance(item, dict) and str(item.get("role") or "") == "user"
    ]
    if not user_indices:
        selected_history: list[dict[str, Any]] = []
    else:
        end = user_indices[-1] + 1
        start = 0
        if mode != "all":
            count = int(mode)
            start = user_indices[max(0, len(user_indices) - count)]
        selected_history = [
            dict(item)
            for item in history[start:end]
            if isinstance(item, dict)
        ]
    return {**snapshot, "history": selected_history}


def _subagent_prompt_cache_fork_diagnostic(
    parent_summary: Any,
    child_summary: Any,
) -> dict[str, Any]:
    return prompt_cache_fork_diagnostic(parent_summary, child_summary)


def _hook_veto(result: Any) -> tuple[bool, str]:
    """Return whether a lifecycle hook vetoed the next state transition."""

    if result is None:
        return False, ""
    blocked = bool(getattr(result, "blocked", False))
    if not blocked:
        return False, ""
    message = str(
        getattr(result, "message", "")
        or getattr(result, "feedback", "")
        or "Hook blocked the lifecycle transition"
    ).strip()
    return True, message


def _is_team_subagent(metadata: dict[str, Any] | None) -> bool:
    """Return whether the child carries teammate ownership metadata."""
    raw = metadata if isinstance(metadata, dict) else {}
    return bool(
        str(raw.get("team_name") or "").strip()
        and str(raw.get("name") or raw.get("teammate_name") or "").strip()
    ) or any(
        str(raw.get(key) or "").strip()
        for key in ("team_name", "team_id", "teammate_name", "teammate_id")
    ) or bool(raw.get("team_mode") or raw.get("is_teammate"))


async def _run_subagent_start_hook(subagent_id: str, agent_type: str) -> Any | None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return None
    try:
        return await hook_mgr.run_subagent_start(
            subagent_id=subagent_id,
            agent_type=agent_type,
        )
    except Exception as exc:
        logger.warning("subagent_start hook failed: %s", exc)
        return None


async def _run_subagent_stop_hook(
    subagent_id: str,
    status: str,
    summary: str = "",
    *,
    agent_type: str = "",
) -> Any | None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return None
    try:
        return await hook_mgr.run_subagent_stop(
            subagent_id=subagent_id,
            agent_type=agent_type,
            status=status,
            summary=summary,
        )
    except Exception as exc:
        logger.warning("subagent_stop hook failed: %s", exc)
        return None


async def _run_task_created_hook(
    *,
    task_id: str,
    subject: str,
    description: str,
    teammate_name: str = "",
    team_name: str = "",
) -> Any | None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return None
    try:
        return await hook_mgr.run_task_created(
            task_id=task_id,
            subject=subject,
            description=description,
            teammate_name=teammate_name,
            team_name=team_name,
        )
    except Exception as exc:
        logger.warning("task_created hook failed: %s", exc)
        return None


async def _run_task_completed_hook(
    *,
    task_id: str,
    subject: str,
    description: str,
    teammate_name: str = "",
    team_name: str = "",
) -> Any | None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return None
    try:
        return await hook_mgr.run_task_completed(
            task_id=task_id,
            subject=subject,
            description=description,
            teammate_name=teammate_name,
            team_name=team_name,
        )
    except Exception as exc:
        logger.warning("task_completed hook failed: %s", exc)
        return None


async def _run_teammate_idle_hook(
    *,
    teammate_name: str = "",
    team_name: str = "",
) -> Any | None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return None
    try:
        return await hook_mgr.run_teammate_idle(
            teammate_name=teammate_name,
            team_name=team_name,
        )
    except Exception as exc:
        logger.warning("teammate_idle hook failed: %s", exc)
        return None


async def _run_terminal_lifecycle_hooks(
    *,
    subagent_id: str,
    status: str,
    summary: str,
    subject: str,
    agent_type: str,
    run_completion: bool = True,
    run_idle: bool = True,
    team_mode: bool = False,
) -> tuple[bool, str]:
    """Run terminal hooks in MiniCode's stop → completed → idle order.

    A veto short-circuits later lifecycle events.  Callers use the returned
    decision to preserve hook feedback in cancellation/error records instead
    of silently discarding a HookResult.
    """

    stop_result = await _run_subagent_stop_hook(
        subagent_id,
        status,
        summary,
        agent_type=agent_type,
    )
    blocked, message = _hook_veto(stop_result)
    if blocked:
        return True, message
    if run_completion:
        completed_result = await _run_task_completed_hook(
            task_id=subagent_id,
            subject=subject,
            description=summary,
            teammate_name=agent_type,
        )
        blocked, message = _hook_veto(completed_result)
        if blocked:
            return True, message
    if run_idle:
        idle_result = await _run_teammate_idle_hook(teammate_name=agent_type)
        blocked, message = _hook_veto(idle_result)
        if blocked:
            return True, message
    return False, ""


