"""Resolved model choices captured by one provider step and its tool calls."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from backend.config import AppConfig
from backend.llm.capabilities import capabilities_for_adapter

if TYPE_CHECKING:
    from backend.llm.base import LLMAdapter
    from backend.llm.model_runtime import ModelRuntime
    from backend.llm.provider_contracts import ModelDefinition


@dataclass(frozen=True, slots=True)
class ModelExecutionSnapshot:
    config: AppConfig
    llm: LLMAdapter
    provider: str
    model: str
    thinking_level: str = "off"
    model_runtime: ModelRuntime | None = None
    available_models: tuple[str, ...] = ()
    models_source: str = ""
    model_info: ModelDefinition | None = None

    def __post_init__(self) -> None:
        # Capture the mutable host configuration once. Frozen model/budget
        # values and process-local adapters/strategies keep their identity.
        object.__setattr__(self, "config", replace(
            self.config, permissions=deepcopy(self.config.permissions),
            feature_flags=deepcopy(self.config.feature_flags),
            config_layer_stack=deepcopy(self.config.config_layer_stack),
        ))

    @classmethod
    def capture(cls, config: AppConfig, llm: LLMAdapter) -> ModelExecutionSnapshot:
        """Adapt an SDK-supplied model once, at the host boundary."""
        capabilities = capabilities_for_adapter(llm)
        return cls(
            config=config, llm=llm,
            provider=(capabilities.provider if capabilities.provider != "unknown" else config.llm.provider),
            model=capabilities.model or config.llm.model,
            thinking_level=llm.current_reasoning_effort() or capabilities.effective_reasoning_effort or config.llm.reasoning_effort or "off",
        )


async def refresh_request_auth(tool_context, context_builder, budget_runtime) -> bool:
    """Replace rejected request credentials while retaining its model choice."""
    from backend.agent.loop_preflight import await_preflight

    owner = tool_context.run_context
    snapshot = tool_context.model_execution
    refreshed = await await_preflight(
        owner.refresh_model_auth(snapshot, True, owner.model_owner_task),
        deadline=budget_runtime.active_phase_deadline(), cancel_event=tool_context.cancel_event,
    )
    if refreshed is snapshot:
        return False
    if owner.model_execution is snapshot:
        owner.model_execution = refreshed
    owner.active_model_execution = refreshed
    tool_context.model_execution = refreshed
    tool_context.llm = refreshed.llm
    context_builder.bind_llm(refreshed.llm)
    return True
