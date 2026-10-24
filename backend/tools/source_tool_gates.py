"""Source utility-tool selection shared by registry construction and each step."""
from __future__ import annotations

from typing import Any

from backend.llm.model_request_policy import source_model_request_capabilities


def source_utility_tool_gates(config: Any, *, model: str = "", definition: Any = None,
                              is_subagent: bool = False) -> dict[str, bool]:
    effective = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
    features = effective.get("features", {})
    supported = source_model_request_capabilities(model or config.llm.model, definition).get("experimental_supported_tools", [])
    reminder = config.feature_flags.enabled("current_time_reminder")
    clock = "clock" in supported
    sleep_config = features.get("sleep_tool", {})
    sleep_mode = sleep_config.get("mode", "model_driven") if isinstance(sleep_config, dict) else "model_driven"
    if sleep_mode not in {"model_driven", "always_on"}:
        raise ValueError("features.sleep_tool.mode must be model_driven or always_on")
    reminder_config = features.get("current_time_reminder", {})
    reminder_sleep = reminder_config.get("sleep_tool", False) if isinstance(reminder_config, dict) else False
    return {
        "update_plan": effective.get("tools", {}).get("update_plan", {}).get("enabled", False),
        "request_user_input_async": not is_subagent and any(name in supported for name in
            ("request_user_input_async", "send_user_message_async")),
        "send_message_to_user_async": not is_subagent and (
            config.feature_flags.enabled("send_message_to_user_async") or "send_message_to_user_async" in supported),
        "curr_time": reminder or clock,
        "sleep": config.feature_flags.enabled("sleep_tool") and (
            sleep_mode == "always_on" or (reminder_sleep if reminder else clock)),
        **{name: config.feature_flags.enabled("goals") for name in ("create_goal", "get_goal", "update_goal")},
    }
