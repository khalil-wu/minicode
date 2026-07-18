from __future__ import annotations

import re

from backend.tools.contracts import ToolIssue
from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult


def classify_tool_issue(tc: ToolCallEvent, result: ToolResult, status: str) -> ToolIssue | None:
    detail = str(result.content or "").strip()
    text = detail.lower()
    tool_name = str(tc.name or "tool")

    if re.search(r"needs generated content|requires generated content|generated args missing", text):
        return ToolIssue(
            error_kind="missing_generated_content",
            user_summary="需要先生成完整内容，再调用写入或编辑工具。",
            developer_detail=detail,
            projection="status",
            model_observation=f"Generate the missing content before retrying {tool_name}.",
        )
    if re.search(r"routing correction|not workspace source files|use read_file|use grep_files|requires an explicit external document source", text):
        return ToolIssue(
            error_kind="routing_error",
            user_summary="已纠正工具路由，请改用适合该资源类型的工具。",
            developer_detail=detail,
            projection="status",
            model_observation=detail,
        )
    if re.search(r"stale evidence|stale source|outdated evidence|evidence is stale|证据过期|过期证据", text):
        return ToolIssue(
            error_kind="stale_evidence",
            user_summary="当前证据可能已经过期，需要刷新或说明证据时间边界。",
            developer_detail=detail,
            projection="warning",
            model_observation="Refresh the evidence or answer with a neutral freshness boundary tied to the specific claim.",
        )
    if re.search(r"disabled for this turn|tool disabled|tool is disabled|工具.*禁用|本轮.*禁用", text):
        return ToolIssue(
            error_kind="tool_disabled",
            user_summary="该工具本轮不可用，请改用当前可用工具或直接说明限制。",
            developer_detail=detail,
            projection="status",
            model_observation=f"The {tool_name} tool is disabled for this turn. Use an available tool or continue without it.",
        )

    if status not in {"error", "failed", "blocked"} and not result.is_error:
        return None
    if re.search(r"missing required|is missing required argument", text):
        return ToolIssue(
            error_kind="validation_error",
            user_summary="工具调用缺少必要参数。",
            developer_detail=detail,
            projection="error",
            model_observation=f"The {tool_name} call is missing required arguments.",
        )
    if re.search(
        r"permission denied|always deny|blocked by policy|requires confirmation|"
        r"不在允许范围|允许的路径|禁止的路径|超出允许|outside (?:the )?(?:allowed|trusted) workspace|outside allowed|forbidden path",
        detail,
        re.I,
    ):
        return ToolIssue(
            error_kind="permission_required",
            user_summary="该工具调用被权限策略阻止。",
            developer_detail=detail,
            projection="approval",
            model_observation=f"The {tool_name} call was blocked by permission policy.",
        )
    if re.search(r"timeout|timed out", text):
        return ToolIssue(
            error_kind="timeout",
            user_summary="工具执行超时。",
            developer_detail=detail,
            projection="warning",
            model_observation=f"The {tool_name} call timed out.",
        )
    if re.search(
        r"file does not exist|no such file or directory|path not found|not a file|is a directory|directory does not exist|cannot read binary|non-utf-?8",
        text,
    ):
        return ToolIssue(
            error_kind="not_found",
            user_summary="目标文件或目录不可读取。",
            developer_detail=detail,
            projection="status",
            model_observation=f"The local target for {tool_name} was not readable. Try another path or continue without it.",
        )
    if re.search(r"repeated|same tool call|no progress|duplicate output|sibling copy|相同关键词|高度相似|重复搜索|相似网页搜索|重复写入", text):
        return ToolIssue(
            error_kind="repeat_guard",
            user_summary="已阻止重复且没有新进展的工具调用。",
            developer_detail=detail,
            projection="warning",
            model_observation="Stop repeating the same tool call; change strategy or answer from available context.",
        )
    return ToolIssue(
        error_kind="execution_error",
        user_summary="",
        developer_detail=detail,
        projection="error",
        model_observation=f"The {tool_name} call failed. Try another approach if possible.",
    )
