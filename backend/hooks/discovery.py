from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from backend.atomic_io import canonical_file_path_key
from backend.hooks.models import (
    HookDefinition,
    HookEvent,
    HookSnapshot,
    HookSource,
    HookTrustStatus,
    normalized_hook_hash,
)
from backend.hooks.policy import event_policy

logger = logging.getLogger(__name__)

_EVENT_KEYS = frozenset(event.value for event in HookEvent)


def discover_hook_snapshot(
    *,
    config_stack: Any,
    workspace_root: Path | None,
    workspace_trusted: bool,
    plugin_sources: Iterable[Mapping[str, Any]] | None = None,
) -> HookSnapshot:
    """Build one immutable, source-aware hook registry for a turn.

    Discovery follows lowest-precedence-first registry ordering. The resulting
    snapshot is the only configuration object consumed for this turn.
    """

    warnings: list[str] = list(getattr(config_stack, "startup_warnings", ()) or ())
    effective = config_stack.effective_config()
    requirements = config_stack.requirements
    managed_disable_all = requirements.disable_all_hooks is True
    effective_disable_all = _effective_bool(
        requirement_value=None,
        effective=effective,
        key="disable_all_hooks",
    )
    managed_only = _effective_bool(
        requirement_value=requirements.allow_managed_hooks_only,
        effective=effective,
        key="allow_managed_hooks_only",
    )
    plugin_only = requirements.restricts_customization_to_plugins("hooks")
    # A user/project disable_all_hooks setting is a customization gate, not an
    # enterprise kill switch. Managed/policy hooks remain executable; model it
    # as managed-only. Only the managed requirement can disable the registry.
    disable_all = managed_disable_all
    if not disable_all and effective_disable_all:
        managed_only = True
    allowed_urls = _effective_string_tuple(
        requirements.allowed_http_hook_urls,
        effective.get("allowed_http_hook_urls"),
    )
    allowed_env_vars = _effective_string_tuple(
        requirements.http_hook_allowed_env_vars,
        effective.get("http_hook_allowed_env_vars"),
    )

    disabled_reason = ""
    if disable_all:
        disabled_reason = "Hooks are disabled by the effective disable_all_hooks policy."
    elif workspace_root is not None and not workspace_trusted:
        disabled_reason = (
            f"{Path(workspace_root).resolve()} is not trusted; executable hooks are disabled "
            "for the interactive workspace."
        )
        warnings.append(disabled_reason)

    states = _hook_states(config_stack)
    entries: list[HookDefinition] = []
    display_order = 0

    managed_hooks = requirements.value_for("hooks")
    managed_source = requirements.source_for("hooks")
    if isinstance(managed_hooks, Mapping):
        source_path = str(getattr(managed_source, "location", "") or "<managed-requirements>/requirements.toml")
        display_order = _append_source(
            entries,
            warnings,
            hook_map=managed_hooks,
            source_path=source_path,
            source=HookSource.MANAGED_REQUIREMENTS,
            is_managed=True,
            workspace_trusted=workspace_trusted,
            states=states,
            display_order=display_order,
        )

    managed_hooks_location = str(getattr(managed_source, "location", "") or "")
    visited_hook_folders: set[str] = set()
    for layer in config_stack.get_layers():
        source, is_managed = _layer_source(layer.source)
        if managed_only and not is_managed:
            continue
        if plugin_only and source != HookSource.PLUGIN and not is_managed:
            continue
        source_path = _layer_source_path(layer.source)
        layer_hooks = layer.config.get("hooks") if isinstance(layer.config, Mapping) else None
        duplicate_managed_projection = (
            is_managed
            and source_path
            and managed_hooks_location
            and source_path in managed_hooks_location.split("; ")
            and isinstance(managed_hooks, Mapping)
            and layer_hooks == managed_hooks
        )
        if isinstance(layer_hooks, Mapping) and not duplicate_managed_projection:
            display_order = _append_source(
                entries,
                warnings,
                hook_map=layer_hooks,
                source_path=source_path,
                source=source,
                is_managed=is_managed,
                workspace_trusted=workspace_trusted,
                states=states,
                display_order=display_order,
            )

        folder = _layer_hook_folder(layer.source)
        if folder is None:
            continue
        folder_key = canonical_file_path_key(folder)
        if folder_key in visited_hook_folders:
            continue
        visited_hook_folders.add(folder_key)
        hook_file = folder / "hooks.json"
        payload = _read_json_object(hook_file, warnings)
        hook_map = payload.get("hooks") if isinstance(payload, Mapping) else None
        if isinstance(hook_map, Mapping):
            display_order = _append_source(
                entries,
                warnings,
                hook_map=hook_map,
                source_path=str(hook_file.resolve()),
                source=source,
                is_managed=is_managed,
                workspace_trusted=workspace_trusted,
                states=states,
                display_order=display_order,
            )

    if plugin_sources is None:
        try:
            from backend.services.plugin_settings_service import (
                load_enabled_plugin_hook_sources,
            )

            plugin_sources = load_enabled_plugin_hook_sources(config_stack=config_stack)
        except Exception as exc:
            warnings.append(f"Failed to discover plugin hooks: {exc}")
            plugin_sources = ()
    for raw_source in plugin_sources:
        plugin_managed = bool(raw_source.get("managed"))
        # The managed-only gate excludes plugin hooks even when plugin
        # enablement was policy-managed. Managed
        # state is not the same as a hook declaration originating in policy.
        if managed_only:
            continue
        hook_map = raw_source.get("hooks")
        if not isinstance(hook_map, Mapping):
            continue
        plugin_root = str(raw_source.get("plugin_root") or "")
        plugin_data_root = str(raw_source.get("plugin_data_root") or "")
        env = {
            "MINICODE_PLUGIN_ROOT": plugin_root,
            "MINICODE_PLUGIN_DATA": plugin_data_root,
        }
        display_order = _append_source(
            entries,
            warnings,
            hook_map=hook_map,
            source_path=str(raw_source.get("source_path") or "<plugin>/hooks.json"),
            source=HookSource.PLUGIN,
            is_managed=plugin_managed,
            workspace_trusted=workspace_trusted,
            states=states,
            display_order=display_order,
            plugin_id=str(raw_source.get("plugin_id") or ""),
            plugin_root=plugin_root,
            plugin_data_root=plugin_data_root,
            env=env,
        )

    # Deduplicate executable declarations by handler payload and ``if``
    # condition. Settings scopes are one namespace (the last merged
    # scope wins), while plugin roots stay isolated so two plugins shipping the
    # same command do not accidentally suppress one another.
    entries = _deduplicate_entries(entries)

    return HookSnapshot(
        entries=tuple(entries),
        warnings=tuple(dict.fromkeys(warnings)),
        config_fingerprint=str(getattr(config_stack, "fingerprint", "") or ""),
        workspace_trusted=workspace_trusted,
        disabled_reason=disabled_reason,
        allowed_http_hook_urls=allowed_urls,
        http_hook_allowed_env_vars=allowed_env_vars,
    )


def snapshot_from_settings(
    settings: Mapping[str, Any],
    *,
    workspace_root: Path | None,
    source_path: str = "<settings>",
) -> HookSnapshot:
    """Build a snapshot for callers that already own one settings object."""

    warnings: list[str] = []
    entries: list[HookDefinition] = []
    disabled = _coerce_bool(settings.get("disable_all_hooks"), False)
    hook_map = settings.get("hooks")
    if isinstance(hook_map, Mapping) and not disabled:
        _append_source(
            entries,
            warnings,
            hook_map=hook_map,
            source_path=source_path,
            source=HookSource.USER,
            is_managed=False,
            workspace_trusted=True,
            states={},
            display_order=0,
        )
    return HookSnapshot(
        entries=tuple(entries),
        warnings=tuple(warnings),
        workspace_trusted=True,
        disabled_reason="Hooks are disabled by disable_all_hooks." if disabled else "",
        allowed_http_hook_urls=_string_tuple_or_none(settings.get("allowed_http_hook_urls")),
        http_hook_allowed_env_vars=_string_tuple_or_none(settings.get("http_hook_allowed_env_vars")),
    )


def _append_source(
    entries: list[HookDefinition],
    warnings: list[str],
    *,
    hook_map: Mapping[str, Any],
    source_path: str,
    source: HookSource,
    is_managed: bool,
    workspace_trusted: bool,
    states: Mapping[str, Mapping[str, Any]],
    display_order: int,
    plugin_id: str = "",
    plugin_root: str = "",
    plugin_data_root: str = "",
    env: Mapping[str, str] | None = None,
) -> int:
    for raw_event_name, raw_groups in hook_map.items():
        event = str(raw_event_name)
        if event not in _EVENT_KEYS or not isinstance(raw_groups, list):
            continue
        for group_index, raw_group in enumerate(raw_groups):
            if not isinstance(raw_group, Mapping):
                continue
            matcher = _optional_text(raw_group.get("matcher"))
            handlers = raw_group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler_index, raw_handler in enumerate(handlers):
                if not isinstance(raw_handler, Mapping):
                    continue
                definition = _definition_from_handler(
                    raw_handler,
                    event=event,
                    matcher=matcher,
                    source_path=source_path,
                    source=source,
                    is_managed=is_managed,
                    workspace_trusted=workspace_trusted,
                    states=states,
                    group_index=group_index,
                    handler_index=handler_index,
                    display_order=display_order,
                    plugin_id=plugin_id,
                    plugin_root=plugin_root,
                    plugin_data_root=plugin_data_root,
                    env=env or {},
                    warnings=warnings,
                )
                if definition is not None:
                    entries.append(definition)
                    display_order += 1
    return display_order


def _definition_from_handler(
    raw_handler: Mapping[str, Any],
    *,
    event: str,
    matcher: str | None,
    source_path: str,
    source: HookSource,
    is_managed: bool,
    workspace_trusted: bool,
    states: Mapping[str, Mapping[str, Any]],
    group_index: int,
    handler_index: int,
    display_order: int,
    plugin_id: str,
    plugin_root: str,
    plugin_data_root: str,
    env: Mapping[str, str],
    warnings: list[str],
) -> HookDefinition | None:
    handler_type = str(raw_handler.get("type") or "command").strip().lower()
    if handler_type not in {"command", "prompt", "agent", "http"}:
        warnings.append(f"Skipping unsupported {handler_type!r} hook in {source_path}")
        return None
    command = str(raw_handler.get("command") or "").strip()
    if sys.platform == "win32":
        command = str(
            raw_handler.get("command_windows")
            or command
        ).strip()
    prompt = str(raw_handler.get("prompt") or "").strip()
    url = str(raw_handler.get("url") or "").strip()
    if handler_type == "command" and not command:
        warnings.append(f"Skipping empty command hook in {source_path}")
        return None
    if handler_type in {"prompt", "agent"} and not prompt:
        warnings.append(f"Skipping empty {handler_type} hook in {source_path}")
        return None
    if handler_type == "http" and not url:
        warnings.append(f"Skipping empty HTTP hook URL in {source_path}")
        return None
    if handler_type == "http" and not event_policy(event).http_allowed:
        warnings.append(f"Skipping HTTP {event} hook in {source_path}: event does not support HTTP hooks")
        return None

    policy = event_policy(event)
    timeout = _positive_float(raw_handler.get("timeout"))
    if timeout is None:
        timeout = policy.default_timeout_seconds
    if policy.max_timeout_seconds is not None and timeout > policy.max_timeout_seconds:
        warnings.append(
            f"Clamping {event} hook timeout to {policy.max_timeout_seconds:g}s in {source_path}"
        )
        timeout = policy.max_timeout_seconds
    async_rewake = _coerce_bool(raw_handler.get("async_rewake"), False)
    run_async = handler_type == "command" and (
        _coerce_bool(raw_handler.get("async"), False) or async_rewake
    )
    if event == "session_end" and run_async:
        warnings.append(f"Running async SessionEnd hook synchronously in {source_path}")
        run_async = False

    normalized = copy.deepcopy(dict(raw_handler))
    normalized["type"] = handler_type
    if handler_type == "command":
        normalized["command"] = command
        normalized.pop("command_windows", None)
    current_hash = normalized_hook_hash(event=event, matcher=matcher, handler=normalized)
    key = f"{source_path}:{event}:{group_index}:{handler_index}"
    state = states.get(key, {})
    enabled = is_managed or state.get("enabled") is not False
    trusted_hash = str(state.get("trusted_hash") or "")
    if is_managed:
        trust_status = HookTrustStatus.MANAGED
    elif trusted_hash and trusted_hash == current_hash:
        trust_status = HookTrustStatus.TRUSTED
    elif trusted_hash:
        trust_status = HookTrustStatus.MODIFIED
    elif workspace_trusted:
        # Workspace trust authorizes project handlers. Per-handler hashes stay
        # available for list/preview and future explicit trust UI.
        trust_status = HookTrustStatus.TRUSTED
    else:
        trust_status = HookTrustStatus.UNTRUSTED

    raw_headers = raw_handler.get("headers")
    headers = (
        {str(name): str(value) for name, value in raw_headers.items()}
        if isinstance(raw_headers, Mapping)
        else {}
    )
    allowed_env = _string_tuple(raw_handler.get("allowed_env_vars"))
    additional_limit = _nonnegative_int(
        raw_handler.get("additional_context_limit")
    )
    if additional_limit is not None and not policy.additional_context:
        warnings.append(
            f"Ignoring additional_context_limit for {event} hook in {source_path}"
        )
        additional_limit = None
    return HookDefinition(
        key=key,
        event=event,
        handler_type=handler_type,
        matcher=matcher,
        source_path=source_path,
        source=source,
        display_order=display_order,
        command=command,
        prompt=prompt,
        url=url,
        headers=headers,
        allowed_env_vars=allowed_env,
        env=env,
        timeout_seconds=timeout,
        run_async=run_async,
        async_rewake=async_rewake,
        once=_coerce_bool(raw_handler.get("once"), False),
        condition=str(raw_handler.get("if") or "").strip(),
        model=str(raw_handler.get("model") or "").strip(),
        shell=str(raw_handler.get("shell") or "").strip().lower(),
        status_message=str(raw_handler.get("status_message") or "").strip(),
        additional_context_limit=additional_limit,
        plugin_id=plugin_id,
        plugin_root=plugin_root,
        plugin_data_root=plugin_data_root,
        is_managed=is_managed,
        enabled=enabled,
        current_hash=current_hash,
        trusted_hash=trusted_hash,
        trust_status=trust_status,
    )


def _hook_states(config_stack: Any) -> dict[str, Mapping[str, Any]]:
    states: dict[str, Mapping[str, Any]] = {}
    for layer in config_stack.get_layers():
        hooks = layer.config.get("hooks") if isinstance(layer.config, Mapping) else None
        raw_states = hooks.get("state") if isinstance(hooks, Mapping) else None
        if not isinstance(raw_states, Mapping):
            continue
        for key, value in raw_states.items():
            if isinstance(value, Mapping):
                states[str(key)] = dict(value)
    return states


def _deduplicate_entries(entries: list[HookDefinition]) -> list[HookDefinition]:
    last_by_key: dict[tuple[str, str, str, str], HookDefinition] = {}
    passthrough: list[HookDefinition] = []
    for entry in entries:
        if entry.handler_type not in {"command", "prompt", "agent", "http"}:
            passthrough.append(entry)
            continue
        namespace = (
            f"plugin:{entry.plugin_id or entry.plugin_root}"
            if entry.source == HookSource.PLUGIN
            else "settings"
        )
        if entry.handler_type == "command":
            payload = f"{entry.shell or 'bash'}\0{entry.command}"
        elif entry.handler_type in {"prompt", "agent"}:
            payload = entry.prompt
        else:
            payload = entry.url
        key = (namespace, entry.event, entry.handler_type, f"{payload}\0{entry.condition}")
        last_by_key[key] = entry
    selected = [*passthrough, *last_by_key.values()]
    selected.sort(key=lambda entry: entry.display_order)
    return selected


def _layer_source(source: Any) -> tuple[HookSource, bool]:
    kind = str(getattr(source, "kind", "unknown") or "unknown")
    mapping = {
        "system": HookSource.SYSTEM,
        "user": HookSource.USER,
        "project": HookSource.PROJECT,
        "policy": HookSource.POLICY,
        "mdm": HookSource.MDM,
        "enterprise_managed": HookSource.ENTERPRISE_MANAGED,
        "session_flags": HookSource.SESSION_FLAGS,
        "legacy_managed_config_file": HookSource.LEGACY_MANAGED_CONFIG_FILE,
        "legacy_managed_config_mdm": HookSource.LEGACY_MANAGED_CONFIG_MDM,
    }
    hook_source = mapping.get(kind, HookSource.UNKNOWN)
    return hook_source, kind in {
        "system",
        "policy",
        "mdm",
        "enterprise_managed",
        "legacy_managed_config_file",
        "legacy_managed_config_mdm",
    }


def _layer_source_path(source: Any) -> str:
    file_path = str(getattr(source, "file", "") or "")
    if file_path:
        return file_path
    project_folder = str(getattr(source, "project_config_folder", "") or "")
    if project_folder:
        return str(Path(project_folder) / "config.toml")
    source_id = str(getattr(source, "source_id", "") or "")
    return f"<{getattr(source, 'kind', 'unknown')}:{source_id}>/config.toml"


def _layer_hook_folder(source: Any) -> Path | None:
    project_folder = str(getattr(source, "project_config_folder", "") or "")
    if project_folder:
        return Path(project_folder)
    file_path = str(getattr(source, "file", "") or "")
    if file_path and Path(file_path).name.casefold() == "config.toml":
        return Path(file_path).parent
    return None


def _read_json_object(path: Path, warnings: list[str]) -> Mapping[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        warnings.append(f"Failed to read hook settings {path}: {exc}")
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        warnings.append(f"Failed to parse hook settings {path}: {exc}")
        return {}
    if not isinstance(value, Mapping):
        warnings.append(f"Hook settings {path} must contain a JSON object")
        return {}
    return value


def _effective_bool(
    *,
    requirement_value: bool | None,
    effective: Mapping[str, Any],
    key: str,
) -> bool:
    if requirement_value is not None:
        return requirement_value
    return _coerce_bool(effective.get(key), False)


def _effective_string_tuple(
    requirement_value: tuple[str, ...] | None,
    effective_value: Any,
) -> tuple[str, ...] | None:
    if requirement_value is not None:
        return requirement_value
    return _string_tuple_or_none(effective_value)


def _string_tuple_or_none(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _string_tuple(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _positive_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _substitute_env(command: str, env: Mapping[str, str]) -> str:
    for key, value in env.items():
        command = command.replace(f"${{{key}}}", value)
    return command
