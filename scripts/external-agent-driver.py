from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _json_lines(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _usage_from(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        "input_tokens": int(value.get("input_tokens") or 0),
        "output_tokens": int(value.get("output_tokens") or 0),
        "cache_read_input_tokens": int(
            value.get("cache_read_input_tokens")
            or value.get("cached_input_tokens")
            or value.get("cache_read_tokens")
            or 0
        ),
        "cache_creation_input_tokens": int(
            value.get("cache_creation_input_tokens")
            or value.get("cache_creation_tokens")
            or 0
        ),
    }


def _merge_usage(current: dict[str, int], candidate: dict[str, int], *, replace: bool = False) -> None:
    for key, amount in candidate.items():
        current[key] = amount if replace else current.get(key, 0) + amount


def _extract_metrics(agent: str, model: str, events: list[dict[str, Any]], elapsed_ms: int) -> dict[str, Any]:
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    tool_ids: set[str] = set()
    failed_tool_ids: set[str] = set()
    search_tool_ids: set[str] = set()
    invalid_search_ids: set[str] = set()
    recovery_ids: set[str] = set()
    cost_usd = 0.0

    for index, event in enumerate(events):
        event_type = str(event.get("type") or "")
        if agent == "codex":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            item_type = str(item.get("type") or "")
            item_id = str(item.get("id") or f"item-{index}")
            if item_type in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                tool_ids.add(item_id)
                command = str(item.get("command") or item.get("name") or "").lower()
                if item_type == "web_search" or any(name in command for name in (" rg ", "grep", "findstr", "search")):
                    search_tool_ids.add(item_id)
                status = str(item.get("status") or "").lower()
                exit_code = item.get("exit_code")
                if status in {"failed", "error", "cancelled"} or (isinstance(exit_code, int) and exit_code != 0):
                    failed_tool_ids.add(item_id)
                    if item_id in search_tool_ids:
                        invalid_search_ids.add(item_id)
            if event_type == "turn.completed":
                _merge_usage(usage, _usage_from(event.get("usage")), replace=True)
            if "retry" in event_type or "recover" in event_type:
                recovery_ids.add(str(event.get("id") or index))
        elif agent == "claude":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_id = str(block.get("id") or f"tool-{index}-{len(tool_ids)}")
                tool_ids.add(tool_id)
                if str(block.get("name") or "").lower() in {"grep", "glob", "websearch"}:
                    search_tool_ids.add(tool_id)
            if event_type == "result":
                _merge_usage(usage, _usage_from(event.get("usage")), replace=True)
                cost_usd = float(event.get("total_cost_usd") or 0.0)
            if event.get("is_error") is True:
                failed_tool_ids.add(str(event.get("tool_use_id") or f"error-{index}"))
            if "retry" in event_type or "recover" in event_type:
                recovery_ids.add(str(event.get("uuid") or index))
        else:
            tool_id = str(event.get("toolCallId") or event.get("tool_call_id") or event.get("id") or f"event-{index}")
            lowered = event_type.lower()
            if "tool_execution_start" in lowered or lowered in {"tool_call", "tool_use"}:
                tool_ids.add(tool_id)
                name = str(event.get("toolName") or event.get("tool_name") or event.get("name") or "").lower()
                if name in {"grep", "find", "ls", "web_search"}:
                    search_tool_ids.add(tool_id)
            if "tool_execution_end" in lowered and event.get("isError") is True:
                failed_tool_ids.add(tool_id)
                if tool_id in search_tool_ids:
                    invalid_search_ids.add(tool_id)
            if "retry" in lowered or "recover" in lowered:
                recovery_ids.add(tool_id)
            candidate = _usage_from(event.get("usage"))
            if any(candidate.values()):
                _merge_usage(usage, candidate, replace=True)

    return {
        "agent": agent,
        "model": model,
        "usage": usage,
        "tool_call_count": len(tool_ids),
        "tool_failure_count": len(failed_tool_ids),
        "invalid_search_count": len(invalid_search_ids),
        "recovery_count": len(recovery_ids),
        "provider_elapsed_ms": elapsed_ms,
        "cost_usd": cost_usd,
    }


def _resolve_executable(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"required agent executable is unavailable: {name}")
    return resolved


def _command(args: argparse.Namespace, prompt: str) -> tuple[list[str], str | None]:
    model_args = ["--model", args.model] if args.model else []
    if args.agent == "codex":
        return [
            _resolve_executable(args.executable or "codex"),
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "workspace-write",
            *model_args,
            "-",
        ], prompt
    if args.agent == "claude":
        return [
            _resolve_executable(args.executable or "claude"),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "auto",
            *model_args,
        ], prompt
    if os.environ.get("MINICODE_EVAL_HOST_ISOLATED") != "1":
        raise RuntimeError("Pi has no OS sandbox; run it in the pinned evaluation container and set MINICODE_EVAL_HOST_ISOLATED=1")
    executable = args.executable or "pi"
    return [
        _resolve_executable(executable),
        "--print",
        "--mode",
        "json",
        "--no-session",
        *model_args,
        prompt,
    ], None


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize Codex, Claude Code, and Pi eval telemetry.")
    parser.add_argument("--agent", required=True, choices=("codex", "claude", "pi"))
    parser.add_argument("--model", default="")
    parser.add_argument("--executable", default="")
    args = parser.parse_args()
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("empty task prompt", file=sys.stderr)
        return 2
    try:
        command, stdin = _command(args, prompt)
        started = time.monotonic()
        completed = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=os.environ.copy(),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if completed.stdout:
            print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")
        metrics = _extract_metrics(args.agent, args.model, _json_lines(completed.stdout), elapsed_ms)
        print(json.dumps({"type": "eval.external.summary", "data": metrics}, ensure_ascii=False))
        return completed.returncode
    except Exception as exc:
        print(json.dumps({"type": "eval.external.error", "data": {"agent": args.agent, "error": str(exc)}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
