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

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
