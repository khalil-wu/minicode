"""One step's native-compiled policies, bound to its actual attachments."""
from __future__ import annotations

from dataclasses import dataclass

from backend.environments.errors import ExecutorProtocolError
from backend.network.policy import NetworkExecutionPolicy, capture_thread_network_policy, policy_input


@dataclass(frozen=True)
class PreparedNetworkStep:
    entries: tuple

    def policy_for(self, environment) -> NetworkExecutionPolicy | None:
        for attachment, policy in self.entries:
            if attachment.environment is environment.environment and attachment.selection == environment.selection:
                return policy
        raise ExecutorProtocolError("The execution attachment is not part of the captured network-policy step")


async def prepare_environment_network_step(*, environment_manager, snapshot, config_stack,
        exec_policy_sources=()) -> PreparedNetworkStep:
    draft = capture_thread_network_policy(config_stack)
    entries = []
    for environment in snapshot.turn_environments():
        if draft.controller is None and draft.requirements is None and environment.config.network_policy is None:
            entries.append((environment, None))
            continue
        runtime = environment_manager.network_runtime()
        captured = policy_input(draft, environment, exec_policy_sources=exec_policy_sources)
        result = await runtime.inspect(captured)
        policy = NetworkExecutionPolicy(environment.selection.environment_id, runtime, captured, result["effectivePolicy"], result["remoteLaunch"]) if result["enabled"] else None
        entries.append((environment, policy))
    return PreparedNetworkStep(tuple(entries))
