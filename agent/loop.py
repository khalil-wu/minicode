from agent.fake_llm import FakeLLM
from agent.real_llm import RealLLMClient
from agent.state import AgentState
from agent.tools import ToolRegistry, build_default_tool_registry
from schemas import ChatResponse, ToolCallRecord


def run_agent_loop(
    message: str,
    max_iterations: int,
    llm: FakeLLM | None = None,
    tool_registry: ToolRegistry | None = None,
    real_llm: RealLLMClient | None = None,
) -> ChatResponse:
    active_llm = llm or FakeLLM()
    active_registry = tool_registry or build_default_tool_registry()
    active_real_llm = real_llm
    state = AgentState(user_message=message, max_iterations=max_iterations)

    lowered = message.lower()
    is_tool_path = (
        lowered.startswith("use echo:")
        or lowered.startswith("summarize:")
        or lowered.startswith("use missing tool:")
    )

    if not is_tool_path:
        try:
            reply = (active_real_llm or RealLLMClient()).generate_reply(message)
        except Exception as exc:
            return ChatResponse(
                reply=f"LLM request failed: {exc}",
                stopped_reason="tool_error",
                iterations=1,
                tool_calls=[],
            )

        return ChatResponse(
            reply=reply,
            stopped_reason="completed",
            iterations=1,
            tool_calls=[],
        )

    while state.iterations < state.max_iterations:
        state.iterations += 1
        decision = active_llm.decide(
            user_message=state.user_message,
            tool_outputs=state.tool_outputs,
        )

        if decision.action == "respond":
            state.reply = decision.response_text or ""
            state.stopped_reason = "completed"
            break

        if decision.action != "tool_call" or not decision.tool_name:
            state.reply = "Model returned an invalid action."
            state.stopped_reason = "invalid_model_action"
            break

        if not active_registry.has_tool(decision.tool_name):
            state.reply = f"Tool '{decision.tool_name}' is not registered."
            state.stopped_reason = "invalid_model_action"
            break

        try:
            output = active_registry.execute(
                decision.tool_name,
                decision.tool_input,
            )
        except Exception as exc:
            state.tool_calls.append(
                ToolCallRecord(
                    tool_name=decision.tool_name,
                    tool_input=decision.tool_input,
                    tool_output=str(exc),
                    status="error",
                )
            )
            state.reply = f"Tool '{decision.tool_name}' failed: {exc}"
            state.stopped_reason = "tool_error"
            break

        state.tool_calls.append(
            ToolCallRecord(
                tool_name=decision.tool_name,
                tool_input=decision.tool_input,
                tool_output=output,
                status="success",
            )
        )
        state.tool_outputs.append(output)
    else:
        state.reply = "Agent stopped after reaching the iteration limit."
        state.stopped_reason = "max_iterations"

    return ChatResponse(
        reply=state.reply,
        stopped_reason=state.stopped_reason or "max_iterations",
        iterations=state.iterations,
        tool_calls=state.tool_calls,
    )
