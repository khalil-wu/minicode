"""Codex AskForApproval config and wire values.

Granular fields and defaults follow protocol.rs and app-server v2/shared.rs.
The category of a request comes from its producer/action, never its label.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

ApprovalFlow = Literal["sandbox_approval", "rules", "skill_approval", "request_permissions", "mcp_elicitations", "mcp_tool_call", "network_access"]


@dataclass(frozen=True, slots=True)
class GranularApprovalConfig:
    sandbox_approval: bool
    rules: bool
    mcp_elicitations: bool
    skill_approval: bool = False
    request_permissions: bool = False

    @classmethod
    def parse(cls, raw: Any, *, label: str = "approval_policy.granular") -> GranularApprovalConfig:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} must be a table/object")
        for name in ("sandbox_approval", "rules", "mcp_elicitations"):
            if name not in raw:
                raise ValueError(f"{label}.{name} is required")
        values = {}
        for name in ("sandbox_approval", "rules", "mcp_elicitations", "skill_approval", "request_permissions"):
            value = raw.get(name, False)
            if not isinstance(value, bool):
                raise ValueError(f"{label}.{name} must be a boolean")
            values[name] = value
        return cls(**values)

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    kind: Literal["on-request", "untrusted", "never", "granular"]
    granular: GranularApprovalConfig | None = None

    @classmethod
    def parse(cls, raw: Any, *, label: str = "approval_policy") -> ApprovalPolicy:
        if isinstance(raw, ApprovalPolicy):
            return raw
        if isinstance(raw, str):
            if raw == "on-failure":
                return cls("on-request")
            if raw in {"on-request", "untrusted", "never"}:
                return cls(raw)
            raise ValueError(f"{label} has unsupported value {raw!r}")
        if isinstance(raw, Mapping) and set(raw) == {"granular"}:
            return cls("granular", GranularApprovalConfig.parse(raw["granular"], label=f"{label}.granular"))
        raise ValueError(f"{label} must be a policy string or a granular table/object")

    @classmethod
    def from_context(cls, context: Any) -> ApprovalPolicy:
        if context.approval_policy == "granular":
            return cls("granular", GranularApprovalConfig.parse(context.granular_approval))
        return cls.parse(context.approval_policy)

    def to_wire(self) -> str | dict[str, dict[str, bool]]:
        return {"granular": self.granular.to_dict()} if self.kind == "granular" else self.kind

    def permits(self, flow: ApprovalFlow) -> bool:
        if self.kind == "never":
            return False
        if self.kind != "granular" or flow in {"mcp_tool_call", "network_access"}:
            return True
        return getattr(self.granular, flow)


def policy_for_mode(mode: str, configured: ApprovalPolicy | None = None) -> ApprovalPolicy:
    """Full access is the explicit builtin Never override; other modes keep config."""
    if mode == "bypass":
        return ApprovalPolicy("never")
    return configured or ApprovalPolicy("on-request")


def rejected_approval_reason(context: Any, flow: ApprovalFlow) -> str | None:
    policy = ApprovalPolicy.from_context(context)
    if policy.permits(flow):
        return None
    if policy.kind == "never":
        return "approval_policy:never"
    return f"approval_policy:granular.{flow}=false"
