import json
from unittest.mock import AsyncMock

import pytest

from backend.services import workspace_service


@pytest.fixture(autouse=True)
def github_repository(monkeypatch):
    monkeypatch.setattr(workspace_service, "github_repository_context", AsyncMock(return_value={
        "is_git_repo": True, "eligible": True, "host": "github.com", "branch": "feature",
    }))


@pytest.mark.asyncio
@pytest.mark.parametrize("output, expected", [
    ("To get started with GitHub CLI, please run: gh auth login", "auth_required"),
    ("HTTP 401: Bad credentials", "auth_required"),
    ("HTTP 403: Resource not accessible by integration", "permission_denied"),
    ("dial tcp: network unreachable", "request_failed"),
])
async def test_pr_query_failure_is_not_an_empty_success(tmp_path, monkeypatch, output, expected):
    monkeypatch.setattr(workspace_service.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(workspace_service, "_run_gh_pr_view", AsyncMock(return_value=(1, output)))
    result = await workspace_service.fetch_git_pr_status_payload(tmp_path)
    assert result["diagnostic"] == output
    assert result["error"] != output
    assert result["error_code"] == expected


@pytest.mark.asyncio
async def test_missing_cli_and_no_open_pr_have_distinct_states(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_service.shutil, "which", lambda _: None)
    assert (await workspace_service.fetch_git_pr_status_payload(tmp_path))["error_code"] == "gh_unavailable"
    monkeypatch.setattr(workspace_service.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(workspace_service, "_run_gh_pr_view", AsyncMock(return_value=(1, 'no pull requests found for branch "feature"')))
    result = await workspace_service.fetch_git_pr_status_payload(tmp_path)
    assert result["pr"] is None
    assert "error" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
async def test_completed_pr_never_drives_automation(tmp_path, monkeypatch, state):
    monkeypatch.setattr(workspace_service, "github_cli_command", lambda: "gh")
    monkeypatch.setattr(workspace_service, "_run_gh_pr_view", AsyncMock(return_value=(0, json.dumps({
        "number": 7, "state": state, "headRefName": "feature", "statusCheckRollup": [],
    }))))
    result = await workspace_service.fetch_git_pr_status_payload(tmp_path)
    assert result["pr"] is None
    assert result["checks"] == []


@pytest.mark.asyncio
async def test_a_real_open_pr_on_main_is_not_hidden_by_its_branch_name(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_service, "github_cli_command", lambda: "gh")
    monkeypatch.setattr(workspace_service, "_run_gh_pr_view", AsyncMock(return_value=(0, json.dumps({
        "number": 7, "state": "OPEN", "headRefName": "main", "statusCheckRollup": [],
    }))))
    assert (await workspace_service.fetch_git_pr_status_payload(tmp_path))["pr"]["number"] == 7


@pytest.mark.asyncio
async def test_auto_merge_does_not_target_a_different_pr_after_the_user_clicked(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_service, "github_cli_command", lambda: "gh")
    monkeypatch.setattr(workspace_service, "_run_gh_pr_view", AsyncMock(return_value=(0, json.dumps({
        "number": 8, "state": "OPEN", "headRefName": "feature", "statusCheckRollup": [],
    }))))
    mutation = AsyncMock()
    monkeypatch.setattr(workspace_service, "_run_gh_pr_merge_auto", mutation)
    result = await workspace_service.set_git_pr_automation_payload(tmp_path, {
        "auto_merge": True, "expected_pr_number": 7, "expected_branch": "feature",
    })
    mutation.assert_not_awaited()
    assert result["automation"]["auto_merge"] is False
    assert result["error_code"] == "auto_merge_failed"


@pytest.mark.asyncio
async def test_successful_pr_query_keeps_actual_check_results(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_service.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(workspace_service, "_run_gh_pr_view", AsyncMock(return_value=(0, json.dumps({
        "number": 7, "title": "Change", "state": "OPEN", "headRefName": "feature",
        "statusCheckRollup": [{"name": "build", "conclusion": "FAILURE"}],
    }))))
    result = await workspace_service.fetch_git_pr_status_payload(tmp_path)
    assert result["pr"]["number"] == 7
    assert result["checks"][0]["status"] == "failure"
    assert "error" not in result
