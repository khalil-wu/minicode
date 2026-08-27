"""Canonical runtime resolution for session and turn tool capability policies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.tools.subagent_context import (
    is_subagent_permission_context,
    resolve_agent_execution_profile,
    subagent_toolset_policy,
)
from backend.tools.toolsets import (
    ACTIVE_TOOLSET_POLICY_METADATA_KEY,
    SESSION_TOOLSET_POLICY_METADATA_KEY,
    ToolsetPolicy,
)


def restore_toolset_policy(value: Any, *, label: str) -> ToolsetPolicy:
    """Restore one policy value or reject a malformed capability boundary."""

    if isinstance(value, ToolsetPolicy):
        return value
    if isinstance(value, Mapping):
        return ToolsetPolicy.from_mapping(value)
    raise ValueError(f"{label} must be a ToolsetPolicy or object")


def resolve_context_toolset_policy(
    permission_context: Any | None,
    metadata: Mapping[str, Any] | None = None,
    *,
    session_policy: ToolsetPolicy | Mapping[str, Any] | None = None,
    prefer_active: bool = True,
) -> ToolsetPolicy:
    """Return the policy shared by discovery, schema derivation, and execution.

    The turn-owned active policy already includes workspace/state attenuation and
    deferred activation, so consumers must use it verbatim when present. When a
    turn has not published one yet, the immutable session ceiling is combined
    with the child execution profile exactly once.
    """

    raw_metadata = metadata if isinstance(metadata, Mapping) else {}
    if prefer_active and ACTIVE_TOOLSET_POLICY_METADATA_KEY in raw_metadata:
        return restore_toolset_policy(
            raw_metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY],
            label="active toolset policy",
        )

    if session_policy is not None:
        base_policy = restore_toolset_policy(
            session_policy,
            label="session toolset policy",
        )
    elif SESSION_TOOLSET_POLICY_METADATA_KEY in raw_metadata:
        base_policy = restore_toolset_policy(
            raw_metadata[SESSION_TOOLSET_POLICY_METADATA_KEY],
            label="session toolset policy",
        )
    else:
        base_policy = ToolsetPolicy.default()

    if not is_subagent_permission_context(permission_context, raw_metadata):
        return base_policy

    profile = resolve_agent_execution_profile(permission_context, raw_metadata)
    child_policy = subagent_toolset_policy(
        permission_mode=str(getattr(permission_context, "mode", "") or ""),
        execution_profile=profile,
    )
    return base_policy.restricted_by(child_policy)
