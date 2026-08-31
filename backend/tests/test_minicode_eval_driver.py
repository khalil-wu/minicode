from backend.artifact.store import ArtifactStore
import asyncio
from pathlib import Path

from backend.evals.minicode_driver import (
    _approve_isolated_eval_call,
    _repository_eval_permission,
    _test_file_snapshot,
    _test_integrity_violations,
)
from backend.permissions.checker import evaluate_permission_decision
from backend.services.tool_registry_factory import build_tool_registry


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
