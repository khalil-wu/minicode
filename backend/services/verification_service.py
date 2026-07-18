from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.services.runtime_control_service import CommandOutcome


@dataclass(frozen=True)
class VerificationPlan:
    command: str
    timeout: float
    error: CommandOutcome | None = None


def prepare_verification_plan(data: dict[str, Any], config: Any) -> VerificationPlan:
    agent_config = getattr(config, "agent", None)
    command = str(getattr(agent_config, "verify_command", "") or "").strip()
    supplied_command = str(data.get("command") or "").strip()
    if supplied_command and supplied_command != command:
        return VerificationPlan(
            command="",
            timeout=120.0,
            error=CommandOutcome(
                "verification.run",
                "Ad hoc verification commands are not allowed. Configure agent.verify_command instead.",
                level="error",
            ),
        )
    if not command:
        return VerificationPlan(
            command="",
            timeout=120.0,
            error=CommandOutcome("verification.run", "No verify command configured.", level="warning"),
        )

    default_timeout = float(getattr(agent_config, "verify_timeout_seconds", 120.0) or 120.0)
    try:
        timeout = float(data.get("timeout_seconds") or default_timeout)
    except (TypeError, ValueError):
        timeout = default_timeout
    return VerificationPlan(command=command, timeout=max(1.0, min(timeout, 600.0)))


def no_workspace_outcome() -> CommandOutcome:
    return CommandOutcome("verification.run", "No workspace is available for verification.", level="error")


def verification_started_event(
    run_id: str,
    *,
    command: str,
    conversation_id: str = "",
) -> AgentEvent:
    return AgentEvent.verification_started(run_id, command=command, conversation_id=conversation_id)


def verification_result_event(
    run_id: str,
    *,
    passed: bool,
    output: str,
    command: str,
    conversation_id: str = "",
) -> AgentEvent:
    return AgentEvent.verification_result(
        run_id,
        passed=passed,
        output=output,
        command=command,
        conversation_id=conversation_id,
    )


def verification_result_outcome(run_id: str, *, passed: bool, output: str) -> CommandOutcome:
    return CommandOutcome(
        "verification.run",
        "Verification passed." if passed else "Verification failed.",
        level="success" if passed else "error",
        data={"run_id": run_id, "passed": passed, "output": output},
    )
