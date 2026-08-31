"""Mechanical migration guard for the Agent kernel seams.

This is intentionally small and deterministic. It catches accidental coupling
while loop.py is being split, the same way a compiler or parity harness acts as
the referee in a large migration.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "backend" / "agent" / "loop.py"
TOOL_RUNNER = ROOT / "backend" / "agent" / "tool_batch_runner.py"
RECOVERY = ROOT / "backend" / "agent" / "recovery_controller.py"
TURN_KERNEL = ROOT / "backend" / "agent" / "turn_kernel.py"
RUNTIME = ROOT / "backend" / "agent" / "runtime.py"
RUN_MANAGER = ROOT / "backend" / "ws" / "run_manager.py"
AGENT_RUNNER = ROOT / "backend" / "ws" / "agent_runner.py"
HANDLER = ROOT / "backend" / "ws" / "handler.py"
DURABLE_QUEUE = ROOT / "backend" / "ws" / "durable_user_queue.py"
FORK_REGISTRY = ROOT / "backend" / "ws" / "fork_registry.py"
CONVERSATION_HANDLER = ROOT / "backend" / "ws" / "handlers" / "conversation.py"
STREAM_ATTEMPT = ROOT / "backend" / "agent" / "stream_attempt.py"
TOOL_TRANSITION = ROOT / "backend" / "agent" / "tool_transition.py"
PROVIDER_PROTOCOL = ROOT / "backend" / "agent" / "provider_protocol.py"
PROVIDER_STREAM_RUNTIME = ROOT / "backend" / "agent" / "provider_stream_runtime.py"
PROVIDER_STREAM_CONTROL = ROOT / "backend" / "agent" / "provider_stream_control.py"
PROVIDER_STREAM_FAILURES = ROOT / "backend" / "agent" / "provider_stream_failures.py"
PROVIDER_STREAM_SETTLEMENT = ROOT / "backend" / "agent" / "provider_stream_settlement.py"
PROVIDER_STREAM_WAIT = ROOT / "backend" / "agent" / "provider_stream_wait.py"
PROVIDER_STREAM_EVENT_DISPATCH = (
    ROOT / "backend" / "agent" / "provider_stream_event_dispatch.py"
)
PROVIDER_STREAM_ERROR_EVENT = (
    ROOT / "backend" / "agent" / "provider_stream_error_event.py"
)
PROVIDER_RESPONSE_RECOVERY = ROOT / "backend" / "agent" / "provider_response_recovery.py"
FINAL_ANSWER_ORCHESTRATOR = ROOT / "backend" / "agent" / "final_answer_orchestrator.py"
TOOL_TURN_RUNTIME = ROOT / "backend" / "agent" / "tool_turn_runtime.py"
TURN_RECOVERY_RUNTIME = ROOT / "backend" / "agent" / "turn_recovery_runtime.py"
LOOP_BOOTSTRAP = ROOT / "backend" / "agent" / "loop_bootstrap.py"
LOOP_COMPONENTS = ROOT / "backend" / "agent" / "loop_components.py"
LOOP_HOOK_PROJECTION = ROOT / "backend" / "agent" / "loop_hook_projection.py"
TURN_ITERATION_ADMISSION = (
    ROOT / "backend" / "agent" / "turn_iteration_admission.py"
)
TURN_ITERATION_EXECUTION = (
    ROOT / "backend" / "agent" / "turn_iteration_execution.py"
)
MAILBOX_DELIVERY = ROOT / "backend" / "agent" / "mailbox_delivery.py"
TOOL_SCHEMA_DERIVATION = ROOT / "backend" / "agent" / "tool_schema_derivation.py"
PRIVATE_LOOP_IMPORT_ROOTS = (ROOT / "backend", ROOT / "tests")
LOOP_LINE_BUDGET = 500
LOOP_BYTE_BUDGET = 24_000
PROVIDER_STREAM_LINE_BUDGET = 500
PROVIDER_STREAM_BYTE_BUDGET = 22_000


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def _private_loop_imports() -> list[str]:
    violations: list[str] = []
    for source_root in PRIVATE_LOOP_IMPORT_ROOTS:
        for path in source_root.rglob("*.py"):
            if path == LOOP:
                continue
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module != "backend.agent.loop":
                    continue
                private_names = sorted(
                    alias.name for alias in node.names if alias.name.startswith("_")
                )
                if private_names:
                    relative = path.relative_to(ROOT)
                    violations.append(
                        f"{relative}: {', '.join(private_names)}"
                    )
            if "backend.agent.loop._" in text:
                relative = path.relative_to(ROOT)
                violations.append(f"{relative}: string/module private loop reference")
    return violations


def main() -> int:
    loop_text = LOOP.read_text(encoding="utf-8")
    turn_kernel_text = TURN_KERNEL.read_text(encoding="utf-8")
    loop_imports = _imports(LOOP)
    runner_imports = _imports(TOOL_RUNNER)
    recovery_imports = _imports(RECOVERY)
    turn_kernel_imports = _imports(TURN_KERNEL)
    stream_attempt_imports = _imports(STREAM_ATTEMPT)
    tool_transition_imports = _imports(TOOL_TRANSITION)
    provider_protocol_imports = _imports(PROVIDER_PROTOCOL)
    provider_stream_imports = _imports(PROVIDER_STREAM_RUNTIME)
    provider_stream_control_imports = _imports(PROVIDER_STREAM_CONTROL)
    provider_stream_failure_imports = _imports(PROVIDER_STREAM_FAILURES)
    provider_stream_settlement_imports = _imports(PROVIDER_STREAM_SETTLEMENT)
    provider_stream_wait_imports = _imports(PROVIDER_STREAM_WAIT)
    provider_stream_event_dispatch_imports = _imports(
        PROVIDER_STREAM_EVENT_DISPATCH
    )
    provider_stream_error_event_imports = _imports(PROVIDER_STREAM_ERROR_EVENT)
    provider_response_recovery_imports = _imports(PROVIDER_RESPONSE_RECOVERY)
    final_answer_orchestrator_imports = _imports(FINAL_ANSWER_ORCHESTRATOR)
    tool_turn_runtime_imports = _imports(TOOL_TURN_RUNTIME)
    turn_recovery_runtime_imports = _imports(TURN_RECOVERY_RUNTIME)
    loop_bootstrap_imports = _imports(LOOP_BOOTSTRAP)
    loop_components_imports = _imports(LOOP_COMPONENTS)
    loop_hook_projection_imports = _imports(LOOP_HOOK_PROJECTION)
    turn_iteration_admission_imports = _imports(TURN_ITERATION_ADMISSION)
    turn_iteration_execution_imports = _imports(TURN_ITERATION_EXECUTION)
    turn_iteration_execution_text = TURN_ITERATION_EXECUTION.read_text(encoding="utf-8")
    mailbox_delivery_imports = _imports(MAILBOX_DELIVERY)
    tool_schema_imports = _imports(TOOL_SCHEMA_DERIVATION)
    runtime_imports = _imports(RUNTIME)
    failures: list[str] = []

    if '__all__ = ["AgentLoopSessionContext", "run_agent_loop"]' not in loop_text:
        failures.append(
            "loop.py must expose only AgentLoopSessionContext and run_agent_loop"
        )
    private_loop_imports = _private_loop_imports()
    if private_loop_imports:
        failures.append(
            "private loop.py compatibility imports are forbidden: "
            + "; ".join(private_loop_imports)
        )

    loop_line_count = len(loop_text.splitlines())
    if loop_line_count > LOOP_LINE_BUDGET:
        failures.append(
            f"loop.py exceeds migration budget: {loop_line_count} > {LOOP_LINE_BUDGET} lines"
        )
    loop_byte_count = len(loop_text.encode("utf-8"))
    if loop_byte_count > LOOP_BYTE_BUDGET:
        failures.append(
            f"loop.py exceeds byte budget: {loop_byte_count} > {LOOP_BYTE_BUDGET} bytes"
        )
    provider_stream_text = PROVIDER_STREAM_RUNTIME.read_text(encoding="utf-8")
    provider_stream_failure_text = PROVIDER_STREAM_FAILURES.read_text(encoding="utf-8")
    provider_stream_settlement_text = PROVIDER_STREAM_SETTLEMENT.read_text(encoding="utf-8")
    provider_stream_line_count = len(provider_stream_text.splitlines())
    if provider_stream_line_count > PROVIDER_STREAM_LINE_BUDGET:
        failures.append(
            "provider_stream_runtime.py exceeds migration budget: "
            f"{provider_stream_line_count} > {PROVIDER_STREAM_LINE_BUDGET} lines"
        )
    provider_stream_byte_count = len(provider_stream_text.encode("utf-8"))
    if provider_stream_byte_count > PROVIDER_STREAM_BYTE_BUDGET:
        failures.append(
            "provider_stream_runtime.py exceeds byte budget: "
            f"{provider_stream_byte_count} > {PROVIDER_STREAM_BYTE_BUDGET} bytes"
        )

    if "backend.agent.tool_batch_runner" in loop_imports:
        failures.append("loop.py must not depend on ToolBatchRunner directly")
    if "backend.agent.tool_batch_runner" not in tool_transition_imports:
        failures.append("tool_transition.py must own ToolBatchRunner")
    if "backend.agent.tool_batch_execution" not in runner_imports:
        failures.append("ToolBatchRunner must own the tool_batch_execution dependency")
    if "execute_tool_batch as _execute_tool_batch" in loop_text:
        failures.append("loop.py must not call execute_tool_batch directly")
    if "backend.agent.loop" in runner_imports:
        failures.append("ToolBatchRunner must not import loop.py")
    if "turn_input_queue" not in turn_kernel_text:
        failures.append("TurnKernel must retain the turn-local input seam")
    if "_legacy_degrade_and_finish" in loop_text:
        failures.append("loop.py must not retain the legacy recovery ladder")
    if "backend.agent.recovery_controller" not in turn_recovery_runtime_imports:
        failures.append("turn_recovery_runtime.py must depend on RecoveryController")
    for dependency, label in (
        ("backend.agent.turn_recovery_runtime", "turn recovery"),
        ("backend.agent.stream_sanitizer", "stream sanitization"),
    ):
        if dependency not in turn_iteration_execution_imports:
            failures.append(
                f"turn_iteration_execution.py must own {label} dependencies"
            )
    if "backend.agent.loop" in recovery_imports:
        failures.append("RecoveryController must not import loop.py")
    if "backend.agent.turn_kernel" not in loop_bootstrap_imports:
        failures.append("loop_bootstrap.py must own TurnKernel construction")
    if "backend.agent.loop" in turn_kernel_imports:
        failures.append("TurnKernel must not import loop.py")
    if "backend.agent.runtime_spans" in loop_imports or "backend.agent.turn_input" in loop_imports:
        failures.append("TurnKernel must own runtime-span and turn-input dependencies")
    if "backend.agent.runtime_spans" not in turn_kernel_imports or "backend.agent.turn_input" not in turn_kernel_imports:
        failures.append("TurnKernel must retain runtime-span and turn-input dependencies")
    if "runtime.complete_run(" in loop_text:
        failures.append("loop.py must not complete runtime records directly")
    if "save_run_checkpoint(" in loop_text or "clear_checkpoints(" in loop_text:
        failures.append("TurnKernel must own terminal checkpoint finalization")
    provider_lifecycle_text = (
        provider_stream_text
        + provider_stream_failure_text
        + provider_stream_settlement_text
    )
    if (
        "start_provider_attempt(" not in provider_stream_text
        or "close_provider_attempt(" not in provider_lifecycle_text
    ):
        failures.append(
            "provider stream lifecycle must bracket attempts through TurnKernel"
        )
    if "start_provider_attempt(" in loop_text or "close_provider_attempt(" in loop_text:
        failures.append("provider attempt lifecycle must not leak back into loop.py")
    if "provider_span_started_at" in loop_text or "provider_first_event_reported" in loop_text:
        failures.append("provider attempt timing state must not live in loop.py")
    for dependency, label in (
        ("backend.agent.stream_attempt", "StreamAttemptState"),
        ("backend.agent.loop_bootstrap", "turn bootstrap"),
        ("backend.agent.loop_components", "runtime component wiring"),
        ("backend.agent.turn_iteration_admission", "iteration admission"),
        ("backend.agent.turn_iteration_execution", "iteration execution"),
    ):
        if dependency not in loop_imports:
            failures.append(f"loop.py must depend on extracted {label}")
    for dependency, label in (
        ("backend.agent.provider_stream_wait", "bounded stream wait"),
        ("backend.agent.provider_stream_event_dispatch", "event dispatch"),
        ("backend.agent.provider_stream_error_event", "provider error events"),
        ("backend.agent.provider_stream_failures", "typed stream failures"),
        ("backend.agent.provider_stream_settlement", "stream settlement"),
    ):
        if dependency not in provider_stream_imports:
            failures.append(
                f"provider_stream_runtime.py must depend on extracted {label}"
            )
    if (
        "backend.agent.provider_stream_control"
        not in provider_stream_event_dispatch_imports
    ):
        failures.append(
            "provider_stream_event_dispatch.py must own stream control transitions"
        )
    for dependency, label in (
        ("backend.agent.provider_stream_runtime", "provider stream runtime"),
        ("backend.agent.provider_response_recovery", "post-stream recovery"),
        ("backend.agent.final_answer_orchestrator", "final-answer orchestrator"),
        ("backend.agent.tool_turn_runtime", "tool-turn runtime"),
    ):
        if dependency not in turn_iteration_execution_imports:
            failures.append(
                f"turn_iteration_execution.py must own extracted {label}"
            )
    if "backend.agent.tool_transition" not in tool_turn_runtime_imports:
        failures.append("tool_turn_runtime.py must own the tool transition")
    if "def _merge_pending_tool_calls" in loop_text:
        failures.append("provider tool-call aggregation must not live in loop.py")
    if "prepare_tool_call_sequence(" in loop_text or "tool_call_is_safe_for_model_history(" in loop_text:
        failures.append("tool transition preparation must not live in loop.py")
    if "prepare_tool_transition(" in loop_text or "append_tool_transition_history(" in loop_text:
        failures.append("loop.py must enter tool batches through ToolTransitionController")
    if "StreamTextState(" not in turn_iteration_execution_text:
        failures.append(
            "turn_iteration_execution.py must create the cancellable StreamTextState"
        )
    if "StreamTextState(" in provider_stream_text:
        failures.append(
            "provider_stream_runtime.py must not hide the current StreamTextState"
        )
    for forbidden in (
        "safe_stream_chat_with_request_metadata(",
        "wait_for_provider_event(",
        "project_provider_text_chunk(",
        "project_non_text_provider_event(",
        "run_bounded_verification(",
        "ToolTransitionController(",
        "ToolExecutionContext(",
        "FinalizationCoordinator.create(",
        "TurnBudgetRuntime(",
        "TurnIterationRuntime(",
    ):
        if forbidden in loop_text:
            failures.append(
                f"extracted responsibility leaked back into loop.py: {forbidden}"
            )
    forbidden_stream_locals = {
        "final_candidate_text",
        "finalizable_stream_text",
        "pending_unphased_text",
        "pending_process_text",
        "live_answer_streamed",
        "speculative_unphased_streamed",
    }
    loop_tree = ast.parse(loop_text)
    assigned_stream_locals: set[str] = set()
    for node in ast.walk(loop_tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in forbidden_stream_locals:
                assigned_stream_locals.add(target.id)
    if assigned_stream_locals:
        failures.append(
            "provider text aggregation locals leaked back into loop.py: "
            + ", ".join(sorted(assigned_stream_locals))
        )
    for imports, label in (
        (stream_attempt_imports, "stream_attempt.py"),
        (tool_transition_imports, "tool_transition.py"),
        (provider_protocol_imports, "provider_protocol.py"),
        (provider_stream_imports, "provider_stream_runtime.py"),
        (provider_stream_control_imports, "provider_stream_control.py"),
        (provider_stream_failure_imports, "provider_stream_failures.py"),
        (provider_stream_settlement_imports, "provider_stream_settlement.py"),
        (provider_stream_wait_imports, "provider_stream_wait.py"),
        (
            provider_stream_event_dispatch_imports,
            "provider_stream_event_dispatch.py",
        ),
        (
            provider_stream_error_event_imports,
            "provider_stream_error_event.py",
        ),
        (provider_response_recovery_imports, "provider_response_recovery.py"),
        (final_answer_orchestrator_imports, "final_answer_orchestrator.py"),
        (tool_turn_runtime_imports, "tool_turn_runtime.py"),
        (turn_recovery_runtime_imports, "turn_recovery_runtime.py"),
        (loop_bootstrap_imports, "loop_bootstrap.py"),
        (loop_components_imports, "loop_components.py"),
        (turn_iteration_admission_imports, "turn_iteration_admission.py"),
        (turn_iteration_execution_imports, "turn_iteration_execution.py"),
        (mailbox_delivery_imports, "mailbox_delivery.py"),
        (tool_schema_imports, "tool_schema_derivation.py"),
    ):
        if "backend.agent.loop" in imports:
            failures.append(f"{label} must not import loop.py")
    for dependency, label in (
        ("backend.agent.loop_preflight", "turn preflight"),
        ("backend.agent.loop_session", "session preparation"),
        ("backend.permissions.context", "tool execution context"),
    ):
        if dependency not in loop_bootstrap_imports:
            failures.append(f"loop_bootstrap.py must own {label}")
    for dependency, label in (
        ("backend.agent.tool_schema_derivation", "tool schema derivation"),
        ("backend.agent.turn_iteration_runtime", "iteration preparation"),
        ("backend.agent.turn_budget_runtime", "budget runtime wiring"),
        ("backend.agent.provider_completion", "provider completion wiring"),
    ):
        if dependency not in loop_components_imports:
            failures.append(f"loop_components.py must own {label}")
    if "backend.agent.loop_hook_projection" not in loop_imports:
        failures.append("loop.py must depend on extracted hook projection")
    if "backend.agent.loop" in loop_hook_projection_imports:
        failures.append("loop_hook_projection.py must not import loop.py")
    if "backend.agent.agent_registry" not in runtime_imports:
        failures.append("AgentRuntime must depend on AgentRegistry")
    run_manager_text = RUN_MANAGER.read_text(encoding="utf-8")
    agent_runner_text = AGENT_RUNNER.read_text(encoding="utf-8")
    handler_text = HANDLER.read_text(encoding="utf-8")
    durable_queue_text = DURABLE_QUEUE.read_text(encoding="utf-8")
    fork_registry_text = FORK_REGISTRY.read_text(encoding="utf-8")
    conversation_handler_text = CONVERSATION_HANDLER.read_text(encoding="utf-8")
    if "DurableUserMessageQueue" not in run_manager_text:
        failures.append("SessionRunManager must persist follow-up queue state")
    if "finish_user_message_dispatch" not in handler_text:
        failures.append("queued dispatch must acknowledge or replay its inflight command")
    if (
        "atomic_write_text" not in durable_queue_text
        or "from backend.atomic_io" not in durable_queue_text
    ):
        failures.append(
            "durable queue writes must use the shared atomic text publisher"
        )
    if '"turn_inputs"' not in durable_queue_text:
        failures.append("promoted turn inputs must remain durable until acknowledged")
    if "acknowledge_consumed_turn_input" not in agent_runner_text:
        failures.append("active turn inputs must be acknowledged after context admission")
    if "uuid4" not in fork_registry_text or "registry.create(" not in conversation_handler_text:
        failures.append("context forks must use stable registry-backed identities")
    if "id(forked)" in conversation_handler_text:
        failures.append("context fork ids must not depend on Python object addresses")

    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1
    print("[OK] Agent kernel boundaries are intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
