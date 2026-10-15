"""Source model support and child request settings, independent of transport."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SOURCE_MODELS = json.loads(Path(__file__).with_name("source-model-request-capabilities.json").read_text(encoding="utf-8"))["models"]


@dataclass(frozen=True)
class ModelRequestPolicy:
    model: str = ""
    reasoning_summary: str | None = None
    verbosity: str | None = None
    service_tier: str | None = None
    service_tiers: tuple[str, ...] = ()
    supports_reasoning_summary_parameter: bool = True
    final_output_json_schema: dict[str, Any] | None = None
    final_output_json_schema_strict: bool = True
    prompt_cache_key_override: str | None = None

    def request_signature(self) -> tuple[Any, ...]:
        return self.reasoning_summary, self.verbosity, self.service_tier_for_request(self.service_tier), self.final_output_json_schema, self.final_output_json_schema_strict, self.prompt_cache_key_override

    def service_tier_for_request(self, tier: str | None) -> str | None:
        return tier if tier != "default" and (tier == "flex" or tier in self.service_tiers) else None


def source_model_request_capabilities(model: str, definition: Any = None) -> dict[str, Any]:
    """Use exact catalog records; configured metadata overrides their fields."""
    capabilities = {"support_verbosity": False, "supports_reasoning_summary_parameter": True, "service_tiers": [],
        **_SOURCE_MODELS.get(model, {})}
    if definition is not None:
        capabilities.update({key: value for key, value in definition.extra.items() if key in {
            "support_verbosity", "default_verbosity", "supports_reasoning_summary_parameter", "service_tiers", "multi_agent_version"}})
    return capabilities


def resolve_model_request_policy(config: Any, *, model: str, definition: Any = None, root_service_tier: str | None = None) -> ModelRequestPolicy:
    effective = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
    capability = source_model_request_capabilities(model, definition)
    tier_enabled = root_service_tier == "flex" or config.feature_flags.enabled("ultrafast_mode" if root_service_tier == "ultrafast" else "fast_mode", True)
    # child_config::apply_spawn_agent_service_tier replaces the role tier with
    # the root user's selected tier, including clearing it when the root is None.
    tier = root_service_tier if tier_enabled else None
    return ModelRequestPolicy(model=model, reasoning_summary=effective.get("model_reasoning_summary") if capability["supports_reasoning_summary_parameter"] else "none",
        verbosity=effective.get("model_verbosity", capability.get("default_verbosity")) if capability["support_verbosity"] else None,
        service_tier=tier, service_tiers=tuple(entry["id"] for entry in capability["service_tiers"]),
        supports_reasoning_summary_parameter=capability["supports_reasoning_summary_parameter"],
        final_output_json_schema=config.model_request_policy.final_output_json_schema if config.model_request_policy is not None else None,
        final_output_json_schema_strict=config.model_request_policy.final_output_json_schema_strict if config.model_request_policy is not None else True,
        prompt_cache_key_override=config.model_request_policy.prompt_cache_key_override if config.model_request_policy is not None else None)
