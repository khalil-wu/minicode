"""Verify backend events.py and frontend events.ts stay in lockstep.

Diffs the backend Literal contracts against the frontend runtime type sets.
Run this in CI or pre-commit. Exits non-zero on drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "backend" / "ws" / "events.py"
TS = ROOT / "frontend" / "src.v2" / "protocol" / "events.ts"
COMMAND_HANDLERS = ROOT / "backend" / "ws" / "command_handlers.py"
DOMAIN_HANDLERS = ROOT / "backend" / "ws" / "handlers"
WS_HANDLER = ROOT / "backend" / "ws" / "handler.py"


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


def parse_registered_backend_commands(*sources: str) -> set[str]:
    combined = "\n".join(sources)
    commands = set(re.findall(r'^\s*"([^"]+)"\s*:\s*handle_', combined, flags=re.MULTILINE))
    commands.update(re.findall(r'command\.type\s*==\s*"([^"]+)"', combined))
    commands.update(re.findall(r'command_registry\.register\("([^"]+)"', combined))
    return commands


def parse_literal_backend_events(*sources: str) -> set[str]:
    combined = "\n".join(sources)
    events = set(re.findall(r'"type"\s*:\s*"([^"]+)"', combined))
    events.update(re.findall(r'AgentEvent\(\s*type="([^"]+)"', combined))
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

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
