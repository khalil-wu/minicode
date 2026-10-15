"""Canonical immutable tool request shared by permission and execution."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping

from backend.llm.base import ToolCallEvent


def canonical_tool_request_digest(tool_name: str, args: Mapping[str, Any]) -> str:
    """Return the stable provider-neutral identity of an executable request.

    The digest intentionally excludes the provider call id.  A retry may get a
    new transport id while still representing the same user-authorized tool
    request.  Internal execution-only fields (for example expected hashes)
    must be added to a detached execution copy and therefore never enter this
    identity.
    """
    payload = {
        "tool_name": str(tool_name or "").strip(),
        "arguments": _canonical_json_value(args),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json_value(value: Any) -> Any:
    """Normalize JSON-shaped values without retaining caller-owned objects."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(
            (_canonical_json_value(item) for item in value),
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    return value


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return deepcopy(value)


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_value(item) for item in value]
    if isinstance(value, (frozenset, set)):
        return {_thaw_value(item) for item in value}
    return deepcopy(value)


@dataclass(frozen=True, slots=True)
class FinalExecutableToolRequest:
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    digest: str
    policy_identity: str = ""
    arguments_digest: str = ""
    exec_policy: Any | None = None

    @classmethod
    def freeze(cls, tc: ToolCallEvent, *, policy_identity: str = "") -> "FinalExecutableToolRequest":
        arguments = _freeze_value(dict(tc.arguments or {}))
        digest = canonical_tool_request_digest(policy_identity or tc.name, _thaw_value(arguments))
        return cls(
            tool_call_id=str(tc.id or "").strip(),
            tool_name=str(tc.name or "").strip(),
            arguments=arguments,
            digest=digest,
            policy_identity=policy_identity or str(tc.name or "").strip(),
            arguments_digest=digest,
        )

    def with_exec_policy(self, decision: Any) -> "FinalExecutableToolRequest":
        return replace(self, digest=decision.command.execution_digest, exec_policy=decision)

    def matches_arguments(self, arguments: Mapping[str, Any]) -> bool:
        return canonical_tool_request_digest(self.policy_identity, arguments) == (self.arguments_digest or self.digest)

    def apply(self, tc: ToolCallEvent) -> None:
        """Replace mutable event arguments with a detached canonical copy."""

        tc.arguments = _thaw_value(self.arguments)

    def argument_values(self) -> dict[str, Any]:
        return _thaw_value(self.arguments)
