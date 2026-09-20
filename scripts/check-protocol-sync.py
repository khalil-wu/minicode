"""Verify backend events.py and frontend events.ts stay in lockstep.

Diffs the backend Literal contracts against the frontend runtime type sets.
Run this in CI or pre-commit. Exits non-zero on drift.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "backend" / "ws" / "events.py"
TS = ROOT / "frontend" / "src.v2" / "protocol" / "events.ts"
COMMAND_HANDLERS = ROOT / "backend" / "ws" / "command_handlers.py"
DOMAIN_HANDLERS = ROOT / "backend" / "ws" / "handlers"
WS_HANDLER = ROOT / "backend" / "ws" / "handler.py"
WS_EVENTS = ROOT / "backend" / "ws" / "events.py"
AGENT_MESSAGE = ROOT / "backend" / "agent" / "message.py"
STREAMING_TYPES = ROOT / "frontend" / "src.v2" / "protocol" / "streaming-types.ts"
PAYLOAD_CONTRACTS = ROOT / "backend" / "ws" / "payload_contracts.py"
USE_WEBSOCKET = ROOT / "frontend" / "src.v2" / "hooks" / "useWebSocket.ts"
RUNTIME_SPANS = ROOT / "backend" / "agent" / "runtime_spans.py"
RUNTIME_RECORDS = ROOT / "backend" / "agent" / "runtime_records.py"
PERMISSION_CHECKER = ROOT / "backend" / "permissions" / "checker.py"
CONVERSATION_MODELS = ROOT / "backend" / "conversations" / "models.py"
PERMISSION_CONTEXT = ROOT / "backend" / "permissions" / "context.py"
PERMISSION_PROFILES = ROOT / "backend" / "permissions" / "profiles.py"
SLASH_COMMANDS = ROOT / "backend" / "commands" / "slash_commands.py"
MODEL_SELECTION = ROOT / "backend" / "llm" / "model_selection.py"
AGENTS_LOADER = ROOT / "backend" / "agents" / "loader.py"
SERVER_EVENT_VALIDATION = ROOT / "frontend" / "src.v2" / "protocol" / "server-event-validation.ts"
STORE_TYPES = ROOT / "frontend" / "src.v2" / "stores" / "types.ts"
COMMON_TYPES = ROOT / "frontend" / "src.v2" / "protocol" / "common-types.ts"


def parse_python_literal(name: str, source: str) -> set[str]:
    pattern = rf"{name}\s*=\s*Literal\[(.*?)\]"
    m = re.search(pattern, source, re.DOTALL)
    if not m:
        sys.exit(f"could not find Literal[{name}] in {PY}")
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def parse_typescript_runtime_set(name: str, source: str) -> set[str]:
    pattern = rf"export const {name}: ReadonlySet<[^>]+>\s*=\s*new Set<[^>]+>\(\[(.*?)\]\);"
    m = re.search(pattern, source, re.DOTALL)
    if not m:
        sys.exit(f"could not find runtime set {name} in {TS}")
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def parse_python_frozenset(name: str, source: str, source_path: Path) -> set[str]:
    pattern = rf"{re.escape(name)}\s*=\s*frozenset\(\s*(?:\{{(.*?)\}})?\s*\)"
    match = re.search(pattern, source, re.DOTALL)
    if not match:
        sys.exit(f"could not find frozenset {name} in {source_path}")
    return set(re.findall(r'[\"\']([^\"\']+)[\"\']', match.group(1) or ""))


def parse_python_string_frozenset(name: str, source: str, source_path: Path) -> set[str]:
    """Collect the string literals of ``NAME = frozenset({...}) | OTHER`` via the AST.

    Unlike the regex parser this ignores comments, so a backtick or quote in a
    comment cannot be mistaken for an entry.
    """
    module = ast.parse(source)
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        return {
            constant.value
            for constant in ast.walk(node.value)
            if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
        }
    sys.exit(f"could not find frozenset {name} in {source_path}")


def parse_session_projection_validator_branches(source: str) -> set[str]:
    tree = ast.parse(source)
    branches: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "validate_session_projection_payload":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Compare):
                continue
            if not isinstance(child.left, ast.Name) or child.left.id != "event_type":
                continue
            for comparator in child.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    branches.add(comparator.value)
                elif isinstance(comparator, (ast.Set, ast.List, ast.Tuple)):
                    branches.update(
                        element.value
                        for element in comparator.elts
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                    )
    if not branches:
        sys.exit("could not find session projection validator branches")
    return branches


def parse_typescript_const_array(name: str, source: str) -> set[str]:
    pattern = rf"export const {re.escape(name)}\s*=\s*\[(.*?)\]\s*as const;"
    match = re.search(pattern, source, re.DOTALL)
    if not match:
        sys.exit(f"could not find const array {name} in {STREAMING_TYPES}")
    return set(re.findall(r'[\"\']([^\"\']+)[\"\']', match.group(1)))


def parse_typescript_set(name: str, source: str, source_path: Path) -> set[str]:
    """``const NAME = new Set([...])`` or ``new Set<string>([...])``."""
    pattern = rf"const {re.escape(name)}\s*=\s*new Set(?:<[^>]+>)?\(\[(.*?)\]\);"
    match = re.search(pattern, source, re.DOTALL)
    if not match:
        sys.exit(f"could not find set {name} in {source_path}")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def parse_typescript_union(name: str, source: str, source_path: Path) -> set[str]:
    """String members of ``export type NAME = "a" | "b" | ...;`` (open ``string`` members ignored)."""
    pattern = rf"export type {re.escape(name)}\s*=\s*(.*?);"
    match = re.search(pattern, source, re.DOTALL)
    if not match:
        sys.exit(f"could not find union type {name} in {source_path}")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def parse_python_literal_in(name: str, source: str, source_path: Path) -> set[str]:
    """``NAME = Literal[...]`` at module level in an arbitrary backend module."""
    pattern = rf"^{re.escape(name)}\s*=\s*Literal\[(.*?)\]"
    match = re.search(pattern, source, re.DOTALL | re.MULTILINE)
    if not match:
        sys.exit(f"could not find Literal {name} in {source_path}")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _string_constants(node: ast.AST) -> set[str]:
    return {
        constant.value
        for constant in ast.walk(node)
        if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
    }


def parse_python_typeddict_field_literal(class_name: str, field: str, source: str) -> set[str]:
    """Literal members of ``field: Literal[...]`` inside ``class class_name``."""
    module = ast.parse(source)
    for node in module.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == field
            ):
                return _string_constants(statement.annotation)
    sys.exit(f"could not find {class_name}.{field} Literal in {PY}")


def parse_python_typeddict_subtypes(source: str) -> set[str]:
    """Every ``subtype: Literal["x"]`` declared on a Control*RequestData class."""
    module = ast.parse(source)
    found: set[str] = set()
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not (node.name.startswith("Control") and node.name.endswith("RequestData")):
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == "subtype"
            ):
                found |= _string_constants(statement.annotation)
    return found


def parse_python_assignment_strings(name: str, source: str, source_path: Path) -> set[str]:
    """String constants of a module-level ``NAME = (...)`` / ``{...}`` assignment."""
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return _string_constants(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return _string_constants(node.value)
    sys.exit(f"could not find assignment {name} in {source_path}")


def parse_python_typeddict_fields(source: str) -> dict[str, set[str]]:
    fields: dict[str, set[str]] = {}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            isinstance(base, ast.Name) and base.id == "TypedDict"
            for base in node.bases
        ):
            continue
        names = {
            statement.target.id
            for statement in node.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
        }
        fields[node.name] = names
    return fields


def parse_typescript_interface_fields(source: str) -> dict[str, set[str]]:
    interfaces: dict[str, set[str]] = {}
    for match in re.finditer(
        r"export\s+interface\s+(\w+)\s*(?:extends\s+[^\{]+)?\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    ):
        interfaces[match.group(1)] = set(
            re.findall(r"^\s*([A-Za-z_$][\w$]*)\??\s*:", match.group(2), re.MULTILINE)
        )
    return interfaces


def compare_typeddict_fields(python_source: str, typescript_source: str) -> list[str]:
    backend = parse_python_typeddict_fields(python_source)
    frontend = parse_typescript_interface_fields(typescript_source)
    errors: list[str] = []
    matched = 0
    for python_name, python_fields in sorted(backend.items()):
        candidates = {
            python_name.removesuffix("Data"),
            python_name.removesuffix("Command"),
        }
        matches = [
            name
            for name in frontend
            if name.removesuffix("Event") in candidates
            or name.removesuffix("Command") in candidates
        ]
        if not matches:
            continue
        matched += 1
        frontend_fields = set().union(*(frontend[name] for name in matches))
        missing = sorted(
            python_fields
            - frontend_fields
            - {"conversation_id", "message_id", "seq"}
        )
        if missing:
            errors.append(
                f"{python_name} -> {', '.join(matches)}: missing fields {', '.join(missing)}"
            )
    if not matched:
        errors.append("no backend TypedDict has a matching frontend interface")
    return errors


def parse_registered_backend_commands(*sources: str) -> set[str]:
    combined = "\n".join(sources)
    commands = set(re.findall(r'^\s*"([^"]+)"\s*:\s*handle_', combined, flags=re.MULTILINE))
    commands.update(re.findall(r'command\.type\s*==\s*"([^"]+)"', combined))
    commands.update(re.findall(r'command_registry\.register\("([^"]+)"', combined))
    return commands


def parse_literal_backend_events(*sources: str) -> set[str]:
    events: set[str] = set()

    def literal_event_type(expression: ast.AST) -> str | None:
        if not isinstance(expression, ast.Dict):
            return None
        for key, value in zip(expression.keys, expression.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "type"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                return value.value
        return None

    def call_name(call: ast.Call) -> str:
        function = call.func
        if isinstance(function, ast.Attribute):
            return function.attr
        if isinstance(function, ast.Name):
            return function.id
        return ""

    def function_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef):
        stack = list(reversed(function.body))
        while stack:
            node = stack.pop()
            yield node
            children = list(ast.iter_child_nodes(node))
            for child in reversed(children):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                stack.append(child)

    def collect_expression(
        expression: ast.AST,
        assignments: dict[str, list[ast.AST]],
        seen_names: set[str] | None = None,
    ) -> None:
        event_type = literal_event_type(expression)
        if event_type is not None:
            events.add(event_type)
            return
        if isinstance(expression, ast.Name):
            visited = set(seen_names or ())
            if expression.id in visited:
                return
            visited.add(expression.id)
            for assigned in assignments.get(expression.id, ()):
                collect_expression(assigned, assignments, visited)
            return
        if not isinstance(expression, ast.Call):
            return
        name = call_name(expression)
        if name == "AgentEvent":
            for keyword in expression.keywords:
                if (
                    keyword.arg == "type"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    events.add(keyword.value.value)
            return
        if name in {"apply", "dict"} and expression.args:
            collect_expression(expression.args[0], assignments, seen_names)

    for source in sources:
        tree = ast.parse(source)
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            assignments: dict[str, list[ast.AST]] = {}
            nodes = tuple(function_nodes(function))
            for node in nodes:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            assignments.setdefault(target.id, []).append(node.value)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                    assignments.setdefault(node.target.id, []).append(node.value)

            for node in nodes:
                if not isinstance(node, ast.Call) or call_name(node) not in {"_send_event", "_send_ws_payload"}:
                    continue
                if node.args:
                    collect_expression(node.args[0], assignments)

            for node in nodes:
                if isinstance(node, ast.Call) and call_name(node) == "AgentEvent":
                    collect_expression(node, assignments)
    return events


def report_drift(title: str, only_left: set[str], only_right: set[str], left_label: str, right_label: str) -> bool:
    if not only_left and not only_right:
        return False
    print(f"\n[DRIFT] {title}")
    if only_left:
        print(f"  only in {left_label}: {sorted(only_left)}")
    if only_right:
        print(f"  only in {right_label}: {sorted(only_right)}")
    return True


def main() -> int:
    py_src = PY.read_text(encoding="utf-8")
    ts_src = TS.read_text(encoding="utf-8")
    command_handlers_src = COMMAND_HANDLERS.read_text(encoding="utf-8")
    ws_handler_src = WS_HANDLER.read_text(encoding="utf-8")
    agent_message_src = AGENT_MESSAGE.read_text(encoding="utf-8")
    streaming_types_src = STREAMING_TYPES.read_text(encoding="utf-8")
    events_src = WS_EVENTS.read_text(encoding="utf-8")
    domain_handler_sources = [
        path.read_text(encoding="utf-8")
        for path in sorted(DOMAIN_HANDLERS.glob("*.py"))
        if path.name != "__init__.py"
    ]

    pairs = [
        ("ServerEventType", "SERVER_EVENT_TYPES"),
        ("ClientCommandType", "CLIENT_COMMAND_TYPES"),
    ]

    drift = False
    parsed: dict[str, set[str]] = {}
    for py_name, ts_name in pairs:
        py = parse_python_literal(py_name, py_src)
        ts = parse_typescript_runtime_set(ts_name, ts_src)
        parsed[py_name] = py
        if report_drift(py_name, py - ts, ts - py, "backend", "frontend"):
            drift = True
        else:
            print(f"[OK] {py_name}: {len(py)} entries match")

    progress_pairs = [
        ("_AGENT_PROGRESS_STAGES", "AGENT_PROGRESS_STAGES"),
        ("_AGENT_PROGRESS_STATUSES", "AGENT_PROGRESS_STATUSES"),
        ("_AGENT_PROGRESS_PHASES", "AGENT_PROGRESS_PHASES"),
    ]
    for py_name, ts_name in progress_pairs:
        py = parse_python_frozenset(py_name, agent_message_src, AGENT_MESSAGE)
        ts = parse_typescript_const_array(ts_name, streaming_types_src)
        if report_drift(ts_name, py - ts, ts - py, "backend", "frontend"):
            drift = True
        else:
            print(f"[OK] {ts_name}: {len(py)} entries match")

    field_errors = compare_typeddict_fields(events_src, streaming_types_src)
    if field_errors:
        for error in field_errors:
            print(f"[DRIFT] TypedDict fields: {error}")
        drift = True
    else:
        print("[OK] Backend TypedDict fields are covered by frontend interfaces")

    registered_commands = parse_registered_backend_commands(
        command_handlers_src,
        ws_handler_src,
        *domain_handler_sources,
    )
    untyped_commands = registered_commands - parsed["ClientCommandType"]
    if untyped_commands:
        drift = True
        print("\n[DRIFT] Backend registered commands missing from ClientCommandType")
        print(f"  missing: {sorted(untyped_commands)}")
    else:
        print(f"[OK] Backend registered commands: {len(registered_commands)} covered")

    literal_events = parse_literal_backend_events(command_handlers_src, ws_handler_src, *domain_handler_sources)
    untyped_events = literal_events - parsed["ServerEventType"]
    if untyped_events:
        drift = True
        print("\n[DRIFT] Backend literal event payloads missing from ServerEventType")
        print(f"  missing: {sorted(untyped_events)}")
    else:
        print(f"[OK] Backend literal event payloads: {len(literal_events)} covered")

    payload_contracts_src = PAYLOAD_CONTRACTS.read_text(encoding="utf-8")
    # The renderer must not move its durable replay cursor on any event the
    # backend never stages into the replay log; otherwise the next durable
    # event's previous_replay_seq mismatches and the stream freezes.
    backend_non_replayable = parse_python_string_frozenset(
        "NON_REPLAYABLE_EVENT_TYPES", payload_contracts_src, PAYLOAD_CONTRACTS,
    ) | parse_python_string_frozenset("LIVE_ONLY_EVENT_TYPES", payload_contracts_src, PAYLOAD_CONTRACTS)
    use_websocket_src = USE_WEBSOCKET.read_text(encoding="utf-8")
    cursor_match = re.search(
        r"const NON_REPLAYABLE_CURSOR_EVENT_TYPES = new Set<string>\(\[(.*?)\]\);",
        use_websocket_src,
        re.DOTALL,
    )
    if not cursor_match:
        sys.exit(f"could not find NON_REPLAYABLE_CURSOR_EVENT_TYPES in {USE_WEBSOCKET}")
    frontend_cursor_set = set(re.findall(r'"([^"]+)"', cursor_match.group(1)))
    if report_drift(
        "NON_REPLAYABLE_CURSOR_EVENT_TYPES",
        backend_non_replayable - frontend_cursor_set,
        frontend_cursor_set - backend_non_replayable,
        "backend",
        "frontend",
    ):
        drift = True
    else:
        print(f"[OK] Non-replayable cursor set: {len(frontend_cursor_set)} entries match")

    projection_events = parse_python_frozenset(
        "SESSION_PROJECTION_EVENT_TYPES",
        payload_contracts_src,
        PAYLOAD_CONTRACTS,
    )
    validated_projection_events = parse_python_frozenset(
        "SESSION_PROJECTION_EVENTS_WITH_VALIDATION",
        payload_contracts_src,
        PAYLOAD_CONTRACTS,
    )
    no_extra_validation_events = parse_python_frozenset(
        "SESSION_PROJECTION_EVENTS_WITHOUT_EXTRA_VALIDATION",
        payload_contracts_src,
        PAYLOAD_CONTRACTS,
    )
    actual_validator_branches = parse_session_projection_validator_branches(
        payload_contracts_src,
    )
    missing_validator_branches = validated_projection_events - actual_validator_branches
    if missing_validator_branches:
        print(
            "\n[DRIFT] Session projection invariant registry has no validator branch: "
            f"{sorted(missing_validator_branches)}"
        )
        drift = True
    if validated_projection_events & no_extra_validation_events:
        overlap = sorted(validated_projection_events & no_extra_validation_events)
        print(f"\n[DRIFT] Session projection invariant registry overlaps: {overlap}")
        drift = True
    acknowledged_projection_events = validated_projection_events | no_extra_validation_events
    if acknowledged_projection_events != projection_events:
        report_drift(
            "Session projection payload invariant coverage",
            projection_events - acknowledged_projection_events,
            acknowledged_projection_events - projection_events,
            "SESSION_PROJECTION_EVENT_TYPES",
            "invariant registry",
        )
        drift = True
    else:
        print(
            "[OK] Session projection payload invariants acknowledged: "
            f"{len(projection_events)} events"
        )

    # ── Value sets shared by both sides beyond the event/command names ──
    def check_pair(title: str, backend: set[str], frontend: set[str]) -> None:
        nonlocal drift
        if report_drift(title, backend - frontend, frontend - backend, "backend", "frontend"):
            drift = True
        else:
            print(f"[OK] {title}: {len(backend)} entries match")

    validation_src = SERVER_EVENT_VALIDATION.read_text(encoding="utf-8")
    runtime_spans_src = RUNTIME_SPANS.read_text(encoding="utf-8")
    runtime_records_src = RUNTIME_RECORDS.read_text(encoding="utf-8")
    store_types_src = STORE_TYPES.read_text(encoding="utf-8")

    check_pair(
        "AGENT_PROGRESS_PROVIDER_STATES",
        parse_python_frozenset("_AGENT_PROGRESS_PROVIDER_STATES", agent_message_src, AGENT_MESSAGE),
        parse_typescript_const_array("AGENT_PROGRESS_PROVIDER_STATES", streaming_types_src),
    )
    for py_name, ts_name in (
        ("_AGENT_MESSAGE_COMPLETION_STATUSES", "AGENT_MESSAGE_COMPLETION_STATUSES"),
        ("_THINKING_LIFECYCLES", "THINKING_LIFECYCLES"),
        ("_AGENT_ITEM_STATUSES", "AGENT_ITEM_STATUSES"),
        ("_AGENT_ITEM_VISIBILITIES", "AGENT_ITEM_VISIBILITIES"),
        ("_DONE_STATUSES", "DONE_STATUSES"),
    ):
        check_pair(
            ts_name,
            parse_python_frozenset(py_name, agent_message_src, AGENT_MESSAGE),
            parse_typescript_set(ts_name, validation_src, SERVER_EVENT_VALIDATION),
        )
    for py_name, ts_name in (
        ("_RUNTIME_SPAN_STATUSES", "RUNTIME_SPAN_STATUSES"),
        ("_TOOL_RUNTIME_SPAN_EVENTS", "TOOL_RUNTIME_SPAN_EVENTS"),
    ):
        check_pair(
            ts_name,
            parse_python_frozenset(py_name, runtime_spans_src, RUNTIME_SPANS),
            parse_typescript_set(ts_name, validation_src, SERVER_EVENT_VALIDATION),
        )

    # The permission mode set is declared five times on the backend; all must
    # agree with the checker's authoritative frozenset and with the renderer.
    permission_modes = parse_python_assignment_strings(
        "PERMISSION_MODES", PERMISSION_CHECKER.read_text(encoding="utf-8"), PERMISSION_CHECKER,
    )
    backend_permission_duplicates = (
        ("ConversationPermissionMode", parse_python_literal_in(
            "ConversationPermissionMode", CONVERSATION_MODELS.read_text(encoding="utf-8"), CONVERSATION_MODELS,
        )),
        ("PermissionMode", parse_python_literal_in(
            "PermissionMode", PERMISSION_CONTEXT.read_text(encoding="utf-8"), PERMISSION_CONTEXT,
        )),
        ("PermissionProductProfile", parse_python_literal_in(
            "PermissionProductProfile", PERMISSION_PROFILES.read_text(encoding="utf-8"), PERMISSION_PROFILES,
        )),
        ("_PERMISSION_MODE_TOKENS", parse_python_assignment_strings(
            "_PERMISSION_MODE_TOKENS", SLASH_COMMANDS.read_text(encoding="utf-8"), SLASH_COMMANDS,
        )),
    )
    for label, duplicate in backend_permission_duplicates:
        if duplicate != permission_modes:
            report_drift(
                f"PERMISSION_MODES vs {label}",
                permission_modes - duplicate, duplicate - permission_modes, "checker", label,
            )
            drift = True
    check_pair(
        "PermissionMode",
        permission_modes,
        parse_typescript_union("PermissionMode", store_types_src, STORE_TYPES),
    )

    # Reasoning effort tokens the renderer may send must be ones the backend orders.
    reasoning_levels = parse_python_assignment_strings(
        "REASONING_LEVEL_ORDER", MODEL_SELECTION.read_text(encoding="utf-8"), MODEL_SELECTION,
    )
    agent_efforts = parse_python_frozenset(
        "_SUPPORTED_AGENT_EFFORT_LEVELS", AGENTS_LOADER.read_text(encoding="utf-8"), AGENTS_LOADER,
    )
    if not agent_efforts <= reasoning_levels:
        report_drift(
            "REASONING_LEVEL_ORDER vs _SUPPORTED_AGENT_EFFORT_LEVELS",
            set(), agent_efforts - reasoning_levels, "model_selection", "agents.loader",
        )
        drift = True
    check_pair(
        "EffortLevel",
        reasoning_levels,
        parse_typescript_union("EffortLevel", store_types_src, STORE_TYPES),
    )

    # Backend TypedDict Literals are what the field-coverage check reads; keep
    # their values equal to the authoritative runtime sets.
    run_statuses = parse_python_literal_in("AgentRunStatus", runtime_records_src, RUNTIME_RECORDS)
    typed_dict_pairs = [
        ("AgentMessageItemData", "status",
         {"in_progress"} | parse_python_frozenset("_AGENT_MESSAGE_COMPLETION_STATUSES", agent_message_src, AGENT_MESSAGE)),
        ("AgentProgressData", "stage", parse_python_frozenset("_AGENT_PROGRESS_STAGES", agent_message_src, AGENT_MESSAGE)),
        ("AgentProgressData", "phase", parse_python_frozenset("_AGENT_PROGRESS_PHASES", agent_message_src, AGENT_MESSAGE)),
        ("AgentProgressData", "status", parse_python_frozenset("_AGENT_PROGRESS_STATUSES", agent_message_src, AGENT_MESSAGE)),
        ("RuntimeSpanData", "status", parse_python_frozenset("_RUNTIME_SPAN_STATUSES", runtime_spans_src, RUNTIME_SPANS)),
        ("AgentItemData", "status", parse_python_frozenset("_AGENT_ITEM_STATUSES", agent_message_src, AGENT_MESSAGE)),
        ("AgentRunData", "status", run_statuses),
        ("AgentRunData", "phase", parse_python_literal_in("AgentRunPhase", runtime_records_src, RUNTIME_RECORDS)),
        ("SubagentDoneData", "status", run_statuses - {"running"}),
    ]
    for class_name, field, authoritative in typed_dict_pairs:
        declared = parse_python_typeddict_field_literal(class_name, field, events_src)
        if declared != authoritative:
            report_drift(
                f"events.py {class_name}.{field}",
                authoritative - declared, declared - authoritative, "runtime set", "events.py",
            )
            drift = True
        else:
            print(f"[OK] events.py {class_name}.{field}: {len(declared)} entries match")

    check_pair(
        "InspectorTargetKind",
        parse_python_typeddict_field_literal("InspectorUpdateData", "target_kind", events_src),
        parse_typescript_union("InspectorTargetKind", store_types_src, STORE_TYPES),
    )
    common_types_src = COMMON_TYPES.read_text(encoding="utf-8")
    # Only the inbound request shapes: ``Control*Request`` interfaces and the
    # ``ControlRequestPayload`` union. ``control_response`` carries its own
    # ``subtype`` and is a client-to-server contract.
    request_shape_blocks = re.findall(
        r"export interface Control\w+Request\s*\{(.*?)\n\}|export type ControlRequestPayload\s*=(.*?);",
        common_types_src,
        re.DOTALL,
    )
    frontend_subtypes = {
        subtype
        for block in request_shape_blocks
        for subtype in re.findall(r'subtype: "([^"]+)"', "".join(block))
    }
    check_pair("ControlRequest subtypes", parse_python_typeddict_subtypes(events_src), frontend_subtypes)

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
