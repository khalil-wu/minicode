from backend.artifact.store import ArtifactStore
import asyncio
import json
import pytest
from pathlib import Path

from backend.evals.minicode_driver import (
    _approve_isolated_eval_call,
    _repository_eval_permission,
    _test_file_snapshot,
    _test_integrity_violations,
)
from backend.permissions.checker import evaluate_permission_decision
from backend.services.tool_registry_factory import build_tool_registry
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
import backend.evals.minicode_driver as driver


def test_repository_eval_exposes_mutation_and_command_tools_without_approval_channel(tmp_path):
    checker, permission = _repository_eval_permission(tmp_path)
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    visible = {
        schema["function"]["name"]
        for schema in registry.get_schemas(
            permission_checker=checker,
            permission_context=permission,
        )
    }
    assert {"run_command", "write_file", "edit_file", "apply_patch"} <= visible

    command = evaluate_permission_decision(
        checker,
        "run_command",
        {"command": "python -m pytest"},
        context=permission,
        tool=registry.get_tool("run_command"),
    )
    assert command.decision == "allow"
    assert command.matched_rule_source == "session_override"

    assert checker.is_path_allowed("source.py", context=permission)
    assert not checker.is_path_allowed(".env", context=permission)


def test_repository_eval_approval_handler_returns_explicit_approval():
    assert asyncio.run(_approve_isolated_eval_call("call-1")) == {"action": "approve"}


def test_external_eval_runs_commands_without_nested_sandbox_and_counts_failed_searches(tmp_path, monkeypatch, capsys):
    from backend.sandbox.policy import SandboxEnforcement
    from backend.tools.command_tool import RunCommandTool

    (tmp_path / "source.py").write_text("MARKER = 42\n", encoding="utf-8")
    policies = []
    execute = RunCommandTool.execute

    async def observe_command(self, args, context=None):
        policies.append(context.sandbox_policy.resolve(cwd=tmp_path).enforcement)
        return await execute(self, args, context)

    class FixtureLLM(LLMAdapter):
        calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                    ToolCallEvent(id="existing-file", name="grep_files", arguments={"path": "source.py", "pattern": "MARKER"}),
                    ToolCallEvent(id="missing-file", name="grep_files", arguments={"path": "missing.py", "pattern": "MARKER"}),
                    ToolCallEvent(id="first-command", name="run_command", arguments={"command": "echo external-boundary-ok"}),
                ])
            elif self.calls == 2:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                    ToolCallEvent(id="next-command", name="run_command", arguments={"command": "echo boundary-still-external"}),
                ])
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Verified the command boundary and searched the requested paths.")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "Verified."

    monkeypatch.setenv("MINICODE_EVAL_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MINICODE_EVAL_API_KEY", "local-eval-fixture")
    monkeypatch.setenv("MINICODE_EVAL_MODEL", "fixture")
    monkeypatch.setenv("MINICODE_EVAL_EXTERNAL_SANDBOX", "true")
    monkeypatch.setattr(driver, "build_wire_adapter", lambda *_args, **_kwargs: FixtureLLM())
    monkeypatch.setattr(driver, "ArtifactStore", lambda: ArtifactStore(storage_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(RunCommandTool, "execute", observe_command)
    assert asyncio.run(driver._run("Search the two paths, run the commands and report their actual outcomes.")) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    results = {record["data"]["id"]: record["data"] for record in records if record.get("type") == "tool_result"}
    assert results["existing-file"]["status"] == "success"
    assert results["missing-file"]["status"] == "failed"
    assert results["first-command"]["status"] == results["next-command"]["status"] == "success"
    assert policies == [SandboxEnforcement.EXTERNAL, SandboxEnforcement.EXTERNAL]
    summary = next(record["data"] for record in records if record.get("type") == "eval.driver.summary")
    assert summary["invalid_search_count"] == 1
    assert summary["tool_call_count"] == 4


def test_repository_eval_detects_modified_or_deleted_existing_tests(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    original = tests / "test_contract.py"
    deleted = tests / "test_deleted.py"
    helper = tests / "helper.py"
    original.write_text("def test_contract():\n    assert True\n", encoding="utf-8")
    deleted.write_text("def test_deleted():\n    assert True\n", encoding="utf-8")
    helper.write_text("VALUE = 1\n", encoding="utf-8")

    snapshot = _test_file_snapshot(tmp_path)
    original.write_text("def test_contract():\n    assert False\n", encoding="utf-8")
    deleted.unlink()
    (tests / "test_new_regression.py").write_text(
        "def test_new_regression():\n    assert True\n",
        encoding="utf-8",
    )

    assert _test_integrity_violations(snapshot, tmp_path) == {
        "modified": [str(Path("tests") / "test_contract.py")],
        "deleted": [str(Path("tests") / "test_deleted.py")],
    }


def test_repository_eval_allows_new_tests_without_changing_existing_tests(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_contract.py").write_text(
        "def test_contract():\n    assert True\n",
        encoding="utf-8",
    )

    snapshot = _test_file_snapshot(tmp_path)
    (tests / "test_new_regression.py").write_text(
        "def test_new_regression():\n    assert True\n",
        encoding="utf-8",
    )

    assert _test_integrity_violations(snapshot, tmp_path) == {
        "modified": [],
        "deleted": [],
    }


@pytest.mark.parametrize("protect,expected_exit", [(True, 1), (False, 0)])
def test_external_oracle_keeps_test_changes_observable_without_failing_a_completed_run(tmp_path, monkeypatch, capsys, protect, expected_exit):
    test_file = tmp_path / "test_original.py"
    test_file.write_text("def test_original():\n    assert True\n")
    class FixtureLLM(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            test_file.write_text("def test_original():\n    assert True\n\ndef test_regression():\n    assert 1 + 1 == 2\n")
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Added regression coverage.")
            yield StreamEvent(type=StreamEventType.DONE)
        async def simple_chat(self, messages):
            return "Added regression coverage."
    monkeypatch.setenv("MINICODE_EVAL_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MINICODE_EVAL_API_KEY", "local-eval-fixture")
    monkeypatch.setenv("MINICODE_EVAL_MODEL", "fixture")
    monkeypatch.setenv("MINICODE_EVAL_PROTECT_EXISTING_TESTS", str(protect))
    monkeypatch.setattr(driver, "build_wire_adapter", lambda *_args, **_kwargs: FixtureLLM())
    monkeypatch.setattr(driver, "ArtifactStore", lambda: ArtifactStore(storage_dir=tmp_path / "artifacts"))
    assert asyncio.run(driver._run("Add regression coverage and report completion.")) == expected_exit
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    summary = next(record["data"] for record in records if record.get("type") == "eval.driver.summary")
    assert summary["test_integrity"]["modified"] == ["test_original.py"]
    assert summary["test_integrity_enforced"] is protect
    assert summary["turn_elapsed_ms"] >= 0
