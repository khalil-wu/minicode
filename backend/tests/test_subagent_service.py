from backend.services.subagent_service import (
    build_subagent_status_event,
    build_subagent_transcript_messages,
)


def test_subagent_status_uses_snapshot_summary_not_internal_result_envelope() -> None:
    event = build_subagent_status_event(
        "subagent-weather",
        {
            "status": "completed",
            "summary": "成都天气调研完成",
            "result": {
                "status": "completed",
                "content": "## Result\n- 成都今天多云。",
            },
        },
    )

    assert event.type == "subagent.done"
    assert event.data["summary"] == "成都天气调研完成"
    assert event.data["result"]["content"].startswith("## Result")


def test_subagent_status_preserves_terminal_metadata() -> None:
    event = build_subagent_status_event(
        "subagent-partial",
        {
            "status": "partial",
            "summary": "部分结果已保留",
            "termination_reason": "deadline_exceeded",
            "timed_out": True,
            "iterations": 4,
            "tool_call_count": 7,
            "result": {"content": "部分内容"},
        },
    )

    assert event.type == "subagent.done"
    assert event.data["status"] == "partial"
    assert event.data["termination_reason"] == "deadline_exceeded"
    assert event.data["timed_out"] is True
    assert event.data["iterations"] == 4
    assert event.data["tool_call_count"] == 7


def test_subagent_status_refresh_keeps_pending_and_blocked_non_terminal() -> None:
    for status in ("pending", "blocked", "running"):
        event = build_subagent_status_event(
            "subagent-live",
            {
                "status": status,
                "detail": f"{status} work",
            },
        )

        assert event.type == "subagent.progress"
        assert event.data["status"] == status
        assert event.data["snapshot"]["status"] == status


def test_subagent_transcript_projects_the_ordinary_chat_schema() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "user_prompt",
            "event_id": "user-1",
            "ts_ms": 1,
            "payload": {
                "content": "检查真实实现",
                "provider_content": "internal delegated system prompt",
            },
        },
        {
            "event_type": "system",
            "event_id": "process-1",
            "ts_ms": 2,
            "payload": {
                "kind": "process_text",
                "content": "正在读取文件",
                "transcript_only": True,
            },
        },
        {
            "event_type": "tool_use",
            "event_id": "tool-1",
            "ts_ms": 3,
            "payload": {"tool_call": {
                "id": "call-1",
                "name": "read_file",
                "arguments": {"path": "README.md"},
                "display_hint": "Read",
                "result_kind": "file",
                "activity_kind": "fileRead",
                "visibility": "timeline",
            }},
        },
        {
            "event_type": "tool_result",
            "event_id": "result-1",
            "ts_ms": 4,
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "status": "completed",
                "content": "file contents",
                "display_summary": "Read README.md",
                "result_kind": "file",
                "activity_kind": "fileRead",
                "visibility": "timeline",
            },
        },
        {
            "event_type": "assistant",
            "event_id": "answer-1",
            "ts_ms": 5,
            "payload": {"content": "检查完成"},
        },
    ]})

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "检查真实实现"
    assert "internal delegated system prompt" not in str(messages)
    assert messages[1]["blocks"][0]["type"] == "process"
    tool_record = next(
        block["record"]
        for block in messages[1]["blocks"]
        if block["type"] == "tool_call"
    )
    assert tool_record["name"] == "read_file"
    assert tool_record["status"] == "success"
    assert tool_record["outputPreview"] == "file contents"
    assert messages[1]["content"] == "检查完成"
    assert messages[1]["is_streaming"] is True


def test_subagent_transcript_coalesces_duplicate_tool_lifecycle_observations() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "tool_use",
            "event_id": "provider-tool-use",
            "ts_ms": 10,
            "payload": {"tool_call": {
                "id": "call-shared",
                "name": "web_fetch",
                "arguments": {"url": "https://example.test/first"},
                "display_hint": "Fetch",
                "result_kind": "web",
                "activity_kind": "webSearch",
                "visibility": "timeline",
            }},
        },
        {
            "event_type": "tool_use",
            "event_id": "execution-tool-start",
            "ts_ms": 12,
            "payload": {"tool_call": {
                "id": "call-shared",
                "name": "web_fetch",
                "arguments": {"url": "https://example.test/final"},
                "display_hint": "Fetch",
                "result_kind": "web",
                "activity_kind": "webSearch",
                "visibility": "timeline",
            }},
        },
        {
            "event_type": "tool_result",
            "event_id": "tool-result",
            "ts_ms": 20,
            "payload": {
                "tool_call_id": "call-shared",
                "tool_name": "web_fetch",
                "status": "success",
                "content": "weather payload",
                "source_url": "https://example.test/final",
                "duration_ms": 10,
                "display_summary": "Fetched example.test",
                "result_kind": "web",
                "activity_kind": "webSearch",
                "visibility": "timeline",
            },
        },
    ]})

    assert len(messages) == 1
    tool_record = messages[0]["blocks"][0]["record"]
    assert tool_record["id"] == "call-shared"
    assert tool_record["args"] == {"url": "https://example.test/final"}
    assert tool_record["startedAt"] == 10
    assert tool_record["finishedAt"] == 20
    assert tool_record["durationMs"] == 10
    assert tool_record["status"] == "success"
    assert tool_record["outputPreview"] == "weather payload"
    assert tool_record["sourceUrl"] == "https://example.test/final"
    assert tool_record["resultKind"] == "web"
    assert tool_record["activityKind"] == "webSearch"


def test_subagent_transcript_projects_live_assistant_text_as_an_ordinary_stream() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "user_prompt",
            "event_id": "user-live",
            "ts_ms": 1,
            "payload": {"content": "实时汇报结果"},
        },
        {
            "event_type": "progress",
            "event_id": "answer-live-1",
            "ts_ms": 2,
            "payload": {
                "kind": "assistant_message",
                "item_id": "answer-live",
                "content": "正在生成",
                "source": "model_final",
                "status": "running",
                "transcript_only": True,
            },
        },
        {
            "event_type": "progress",
            "event_id": "answer-live-2",
            "ts_ms": 3,
            "payload": {
                "kind": "assistant_message",
                "item_id": "answer-live",
                "content": "正在生成最终答复",
                "source": "model_final",
                "status": "running",
                "transcript_only": True,
            },
        },
    ]})

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assistant = messages[1]
    assert assistant["is_streaming"] is True
    assert assistant["content"] == "正在生成最终答复"
    assert assistant["blocks"] == [{
        "type": "text",
        "item_id": "answer-live",
        "content": "正在生成最终答复",
        "source": "model_final",
        "status": "running",
        "is_streaming": True,
    }]


def test_synthetic_missing_tool_result_never_replays_as_success() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "tool_use",
            "event_id": "tool-use",
            "ts_ms": 1,
            "payload": {"tool_call": {
                "id": "call-missing",
                "name": "read_file",
                "arguments": {"path": "README.md"},
                "display_hint": "Read",
                "result_kind": "file",
                "activity_kind": "fileRead",
                "visibility": "timeline",
            }},
        },
        {
            "event_type": "tool_result",
            "event_id": "tool-result",
            "ts_ms": 2,
            "payload": {
                "tool_call_id": "call-missing",
                "tool_name": "read_file",
                # Compatibility with journals written before the repair status
                # was made explicitly erroneous.
                "status": "completed",
                "content": "[Tool result missing due to internal error]",
                "synthetic": True,
            },
        },
    ]})

    tool_record = messages[0]["blocks"][0]["record"]
    assert tool_record["status"] == "failed"
    assert tool_record["outputPreview"] == "[Tool result missing due to internal error]"


def test_subagent_transcript_attaches_terminal_duration_to_the_single_turn() -> None:
    messages = build_subagent_transcript_messages({"agent_id": "child-1", "events": [
        {
            "event_type": "user_prompt",
            "event_id": "user-1",
            "ts_ms": 100,
            "payload": {"content": "完成任务"},
        },
        {
            "event_type": "tool_use",
            "event_id": "tool-1",
            "ts_ms": 120,
            "payload": {"tool_call": {
                "id": "call-1",
                "name": "list_files",
                "arguments": {"path": "."},
                "display_hint": "List",
                "result_kind": "file",
                "activity_kind": "workspaceList",
                "visibility": "timeline",
            }},
        },
        {
            "event_type": "tool_result",
            "event_id": "result-1",
            "ts_ms": 220,
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "list_files",
                "status": "success",
                "content": "a.ts",
                "result_kind": "file",
                "activity_kind": "workspaceList",
                "visibility": "timeline",
            },
        },
        {
            "event_type": "assistant",
            "event_id": "answer-1",
            "ts_ms": 300,
            "payload": {"content": "完成"},
        },
        {
            "event_type": "terminal",
            "event_id": "terminal-1",
            "ts_ms": 500,
            "payload": {"status": "completed", "duration_ms": 400, "reason": "success"},
        },
    ]})

    assert len(messages) == 2
    assistant = messages[1]
    assert assistant["completed_at"] == 500
    assert assistant["duration_ms"] == 400
    assert assistant["terminal_status"] == "completed"
    assert assistant["is_streaming"] is False
    assert assistant["content"] == "完成"
    record = next(block["record"] for block in assistant["blocks"] if block["type"] == "tool_call")
    assert record["activityKind"] == "workspaceList"


def test_subagent_transcript_preserves_terminal_failure_evidence() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "user_prompt",
            "event_id": "user-1",
            "ts_ms": 100,
            "payload": {"content": "完成任务"},
        },
        {
            "event_type": "terminal",
            "event_id": "terminal-1",
            "ts_ms": 500,
            "payload": {
                "status": "failed",
                "duration_ms": 400,
                "reason": "RuntimeError",
                "summary": "RuntimeError: 子任务未完成",
            },
        },
    ]})

    assistant = messages[1]
    assert assistant["terminal_status"] == "failed"
    assert assistant["failure_message"] == "RuntimeError: 子任务未完成"
    assert assistant["duration_ms"] == 400
    assert assistant["is_streaming"] is False


def test_subagent_transcript_uses_durable_runtime_error_as_failed_turn_evidence() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "user_prompt",
            "event_id": "user-1",
            "ts_ms": 100,
            "payload": {"content": "完成任务"},
        },
        {
            "event_type": "system",
            "event_id": "error-1",
            "ts_ms": 300,
            "payload": {
                "lifecycle": "error",
                "message": "RuntimeError: 子任务未完成",
                "error_type": "runtime",
                "recoverable": False,
            },
        },
        {
            "event_type": "terminal",
            "event_id": "terminal-1",
            "ts_ms": 500,
            "payload": {
                "status": "failed",
                "duration_ms": 400,
                "reason": "runtime_error",
            },
        },
    ]})

    assistant = messages[1]
    assert assistant["terminal_status"] == "failed"
    assert assistant["failure_message"] == "RuntimeError: 子任务未完成"
    assert assistant["content"] == ""
    assert assistant["blocks"] == []


def test_subagent_transcript_preserves_main_chat_tool_projection_evidence() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "tool_use",
            "event_id": "edit-use",
            "ts_ms": 10,
            "payload": {"tool_call": {
                "id": "edit-1",
                "name": "edit_file",
                "arguments": {"path": "src/app.ts", "old": "a", "new": "b"},
                "display_hint": "Edit",
                "result_kind": "edit",
                "activity_kind": "fileChange",
                "visibility": "timeline",
                "group_id": "iteration-1",
                "step_id": "edit-1",
            }},
        },
        {
            "event_type": "tool_result",
            "event_id": "edit-result",
            "ts_ms": 35,
            "payload": {
                "tool_call_id": "edit-1",
                "tool_name": "edit_file",
                "status": "success",
                "content": "Edited src/app.ts",
                "duration_ms": 25,
                "display_summary": "Edited src/app.ts",
                "result_kind": "edit",
                "activity_kind": "fileChange",
                "visibility": "timeline",
                "group_id": "iteration-1",
                "step_id": "edit-1",
                "diff": {
                    "plus": 1,
                    "minus": 1,
                    "files": [{
                        "path": "src/app.ts",
                        "plus": 1,
                        "minus": 1,
                        "patch": "@@ -1 +1 @@\n-a\n+b",
                    }],
                },
                "output_files": [{"path": "src/app.ts", "size": 12}],
                "cleanup_receipt": {
                    "resource_kind": "checkpoint",
                    "completed": True,
                },
            },
        },
    ]})

    record = messages[0]["blocks"][0]["record"]
    assert record["resultKind"] == "edit"
    assert record["activityKind"] == "fileChange"
    assert record["groupId"] == "iteration-1"
    assert record["stepId"] == "edit-1"
    assert record["durationMs"] == 25
    assert record["diff"]["files"][0]["path"] == "src/app.ts"
    assert record["outputFiles"] == [{"path": "src/app.ts", "size": 12}]
    assert record["cleanupReceipt"]["completed"] is True


def test_subagent_transcript_preserves_structured_tool_failure() -> None:
    messages = build_subagent_transcript_messages({"events": [
        {
            "event_type": "tool_use",
            "event_id": "fetch-use",
            "ts_ms": 10,
            "payload": {"tool_call": {
                "id": "fetch-1",
                "name": "web_fetch",
                "arguments": {"url": "https://example.test"},
                "display_hint": "Fetch",
                "result_kind": "web",
                "activity_kind": "webSearch",
                "visibility": "timeline",
            }},
        },
        {
            "event_type": "tool_result",
            "event_id": "fetch-result",
            "ts_ms": 40,
            "payload": {
                "tool_call_id": "fetch-1",
                "tool_name": "web_fetch",
                "status": "failed",
                "content": "HTTP 503",
                "duration_ms": 30,
                "source_url": "https://example.test",
                "result_kind": "web",
                "activity_kind": "webSearch",
                "visibility": "timeline",
                "error_kind": "provider_unavailable",
                "user_summary": "网页暂时不可用",
                "developer_detail": "upstream returned 503",
                "recoverable": True,
                "error_info": {"status": 503},
            },
        },
    ]})

    record = messages[0]["blocks"][0]["record"]
    assert record["status"] == "failed"
    assert record["sourceUrl"] == "https://example.test"
    assert record["errorKind"] == "provider_unavailable"
    assert record["userSummary"] == "网页暂时不可用"
    assert record["developerDetail"] == "upstream returned 503"
    assert record["recoverable"] is True
    assert record["errorInfo"] == {"status": 503}


def test_subagent_transcript_requires_complete_tool_identity() -> None:
    import pytest

    with pytest.raises(ValueError, match="has no tool name"):
        build_subagent_transcript_messages({"events": [{
            "event_type": "tool_use",
            "event_id": "bad-use",
            "ts_ms": 1,
            "payload": {"tool_call": {"id": "call-1", "arguments": {}}},
        }]})

    with pytest.raises(ValueError, match="no matching tool use or tool name"):
        build_subagent_transcript_messages({"events": [{
            "event_type": "tool_result",
            "event_id": "bad-result",
            "ts_ms": 1,
            "payload": {"tool_call_id": "call-1", "status": "failed"},
        }]})
