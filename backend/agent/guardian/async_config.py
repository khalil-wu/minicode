"""Source Guardian V2 local/model policy and classifier configuration."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
import math

from backend.agent.guardian.prompt import POLICY
from backend.agent.guardian.truncation import truncate_guardian_text

CLASSIFIER_MODEL = "gpt-5.6-luna"
OUTPUT_CONTRACT = "Your first output token is the entire classification: `high` for high risk or `low` for low risk. Output that token immediately and nothing else."
CLASSIFIER_INSTRUCTIONS = (Path(__file__).with_name("resources") / "async-classifier-instructions.md").read_text(encoding="utf-8")
SCOPES = ("computer_use", "shell", "file_changes", "mcp", "network", "permissions")
MODES = {"disabled", "synchronous", "adaptive"}


@dataclass(frozen=True)
class GuardianModelPolicy:
    scopes: tuple[tuple[str, str], ...]
    other_tools: str = "disabled"
    unscored_action: str = "invalidate_score"
    initial_cua_call: bool | None = None
    sandboxed_exec_commands: bool | None = None

    def mode(self, scope: str | None) -> str:
        return self.other_tools if scope is None else dict(self.scopes).get(scope, "disabled")

    @property
    def scoring_enabled(self) -> bool:
        return any(mode == "adaptive" for _, mode in self.scopes)

    @property
    def allows_initial_cua_call(self) -> bool:
        return self.initial_cua_call if self.initial_cua_call is not None else self.mode("computer_use") == "adaptive"

    def disable_scoring(self):
        return replace(self, scopes=tuple((scope, "synchronous" if mode == "adaptive" else mode) for scope, mode in self.scopes),
            other_tools="synchronous")


def resolve_guardian_model_policy(config, model_info) -> GuardianModelPolicy:
    settings = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
    raw = settings.get("features", {}).get("guardianv2")
    local = raw if isinstance(raw, dict) else {}
    scope = local.get("review_scope") or {}
    computer_only = scope.get("computer_use_only", True)
    other = "synchronous" if computer_only else "adaptive"
    default = GuardianModelPolicy(tuple((name, "adaptive" if name == "computer_use" else other) for name in SCOPES),
        other_tools=other, unscored_action="ignore" if computer_only else "age_score", initial_cua_call=computer_only,
        sandboxed_exec_commands=not computer_only and scope.get("sandboxed_exec_commands", False))
    catalog = model_info.extra.get("guardian") if model_info is not None else None
    if catalog is not None:
        policy = GuardianModelPolicy(tuple((name, catalog.get(name, "disabled") if catalog.get(name, "disabled") in MODES else "unknown") for name in SCOPES),
            other_tools=catalog.get("other_tools", "disabled") if catalog.get("other_tools", "disabled") in MODES else "unknown",
            unscored_action=catalog.get("unscored_action", "invalidate_score") if catalog.get("unscored_action", "invalidate_score") in {"ignore", "age_score", "invalidate_score"} else "invalidate_score",
            initial_cua_call=catalog.get("initial_cua_call"), sandboxed_exec_commands=catalog.get("sandboxed_exec_commands"))
    else:
        policy = default
        if policy.allows_initial_cua_call and model_info is not None and not model_info.extra.get("node_repl_auto_review_required", False):
            policy = replace(policy, scopes=tuple((name, "synchronous" if name == "computer_use" else mode) for name, mode in policy.scopes),
                initial_cua_call=False, unscored_action="invalidate_score")
    required = config.config_layer_stack.requirements if config.config_layer_stack is not None else None
    force_synchronous = required is not None and not required.can_select_user_reviewer()
    if force_synchronous or (not config.feature_flags.enabled("guardianv2") and catalog is None):
        policy = policy.disable_scoring()
    if required is not None and model_info is not None and required.auto_review_required_for_model(model_info.id):
        computer_use = policy.mode("computer_use")
        initial_cua = policy.allows_initial_cua_call
        policy = policy.disable_scoring()
        if initial_cua:
            policy = replace(policy, scopes=tuple((scope, computer_use if scope == "computer_use" else mode) for scope, mode in policy.scopes))
    return policy


def _bounded(value, default, name):
    result = default if value is None else value
    if isinstance(result, bool) or not isinstance(result, int) or not 100 <= result <= 100000:
        raise ValueError(f"Guardian v2 {name} must be between 100 and 100000 tokens")
    return result


@dataclass(frozen=True)
class GuardianAsyncConfig:
    classifier_mode: str = "snapshot"
    instructions: str = CLASSIFIER_INSTRUCTIONS
    review_threshold: float = .5
    max_tool_call_lag: int = 2
    reasoning_effort: str = "low"
    max_action_tokens: int = 10000
    max_classifier_instruction_tokens: int | None = None
    reuse_parent_compaction: bool = True
    max_parent_compaction_tokens: int = 25000
    persist_scores: bool = False
    include_images: bool = True
    sources: tuple[str, ...] = ("tool_calls", "tool_outputs")
    max_message_entry_tokens: int = 2000
    max_tool_entry_tokens: int = 1000
    max_message_transcript_tokens: int = 10000
    max_tool_transcript_tokens: int = 10000
    max_recent_non_user_entries: int = 40
    conversation_token_limit: int = 100000

    @classmethod
    def resolve(cls, config, model_info):
        settings = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
        raw = settings.get("features", {}).get("guardianv2")
        local = raw if isinstance(raw, dict) else {}
        messages = model_info.model_messages if model_info is not None else config.llm.model_messages
        model = (messages or {}).get("guardian_v2") or {}
        def pick(name, default=None):
            return local[name] if local.get(name) is not None else model.get(name, default)
        mode = pick("async_classifier_mode", "snapshot")
        if mode not in {"snapshot", "conversation"}:
            raise ValueError("invalid Guardian v2 async_classifier_mode")
        conversation_limit = pick("async_classifier_conversation_token_limit", 100000)
        if isinstance(conversation_limit, bool) or not isinstance(conversation_limit, int) or conversation_limit <= 0:
            raise ValueError("Guardian v2 async_classifier_conversation_token_limit must be positive")
        custom_instructions = pick("classifier_instructions")
        threshold = (local.get("review_threshold") if local.get("review_threshold") is not None else
            model["review_threshold_basis_points"] / 10000 if model.get("review_threshold_basis_points") is not None else
            .8 if custom_instructions is not None else .5)
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Guardian v2 review_threshold must be between 0.0 and 1.0")
        lag = pick("max_tool_call_lag", 2)
        if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0:
            raise ValueError("Guardian v2 max_tool_call_lag must be nonnegative")
        transcript = {**(model.get("transcript") or {}), **{key: value for key, value in (local.get("transcript") or {}).items() if value is not None}}
        sources = tuple(transcript.get("sources", ("tool_calls", "tool_outputs")))
        if any(source not in {"tool_calls", "tool_outputs", "reasoning"} for source in sources):
            raise ValueError("invalid Guardian v2 transcript source")
        message_entry = _bounded(transcript.get("max_message_entry_tokens"), 2000, "max_message_entry_tokens")
        tool_entry = _bounded(transcript.get("max_tool_entry_tokens"), 1000, "max_tool_entry_tokens")
        message_total = _bounded(transcript.get("max_message_transcript_tokens"), 10000, "max_message_transcript_tokens")
        tool_total = _bounded(transcript.get("max_tool_transcript_tokens"), 10000, "max_tool_transcript_tokens")
        if message_entry > message_total or tool_entry > tool_total:
            raise ValueError("Guardian v2 transcript entry tokens must not exceed transcript tokens")
        recent = transcript.get("max_recent_non_user_entries", 40)
        if isinstance(recent, bool) or not isinstance(recent, int) or recent <= 0:
            raise ValueError("Guardian v2 transcript max_recent_non_user_entries must be positive")
        instruction_cap = pick("max_classifier_instruction_tokens")
        return cls(mode, custom_instructions if custom_instructions is not None else CLASSIFIER_INSTRUCTIONS,
            threshold, lag, pick("reasoning_effort", "low"), _bounded(pick("max_action_tokens"), 10000, "max_action_tokens"),
            _bounded(instruction_cap, instruction_cap, "max_classifier_instruction_tokens") if instruction_cap is not None else None,
            pick("reuse_parent_compaction", True), _bounded(pick("max_parent_compaction_tokens"), 25000, "max_parent_compaction_tokens"),
            local.get("persist_scores", False), transcript.get("include_images", True), sources,
            message_entry, tool_entry, message_total, tool_total, recent, conversation_limit)

    def render_instructions(self, settings, model_info=None):
        messages = (model_info.model_messages or {}).get("auto_review", {}) if model_info is not None else {}
        policy = settings.get("guardian_policy_config") or (settings.get("auto_review") or {}).get("policy") or messages.get("policy") or POLICY
        extra = settings.get("guardian_extra_policy") or (settings.get("auto_review") or {}).get("extra_policy") or ""
        if extra.strip():
            policy = f"{policy}\n\n{extra.strip()}"
        rendered = (self.instructions.replace("{{ tenant_policy_config }}", policy) if "{{ tenant_policy_config }}" in self.instructions else
            f"{self.instructions}\n\n# Security Policy\n{policy}")
        if not rendered.rstrip().endswith(OUTPUT_CONTRACT):
            rendered += f"\n\n{OUTPUT_CONTRACT}"
        return truncate_guardian_text(rendered, self.max_classifier_instruction_tokens) if self.max_classifier_instruction_tokens is not None else rendered
