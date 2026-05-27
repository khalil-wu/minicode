"""Realtime search policy for time-sensitive user requests."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from backend.agent.state import AgentState
    from backend.llm.base import ToolCallEvent
    from backend.tools.base import ToolResult


_EXPLICIT_SEARCH_TERMS: tuple[str, ...] = (
    "联网",
    "上网",
    "网上",
    "搜索",
    "搜一下",
    "查一下",
    "查查",
    "查最新",
    "查新闻",
    "web search",
    "search web",
    "browse",
    "look up",
    "google",
)

_TEMPORAL_TERMS: tuple[str, ...] = (
    "实时",
    "最新",
    "近期",
    "近日",
    "最近",
    "刚刚",
    "当前",
    "现在",
    "今天",
    "今日",
    "今晚",
    "今夜",
    "昨天",
    "明天",
    "本周",
    "这周",
    "本月",
    "今年",
    "截至",
    "目前",
    "recent",
    "latest",
    "breaking",
    "current",
    "now",
    "today",
    "today's",
    "yesterday",
    "tomorrow",
    "this week",
    "this month",
    "as of",
)

_REALTIME_DOMAINS: tuple[str, ...] = (
    "天气",
    "温度",
    "气温",
    "下雨",
    "空气质量",
    "预报",
    "新闻",
    "消息",
    "官宣",
    "通报",
    "突发",
    "事故",
    "爆炸",
    "地震",
    "火灾",
    "航班",
    "列车",
    "赛程",
    "比分",
    "排名",
    "股价",
    "汇率",
    "金价",
    "油价",
    "比特币",
    "加密货币",
    "财报",
    "利率",
    "关税",
    "制裁",
    "政策",
    "法规",
    "法律",
    "选举",
    "民调",
    "总统",
    "首相",
    "总理",
    "主席",
    "外交",
    "访问",
    "会晤",
    "访华",
    "访美",
    "发布",
    "版本",
    "更新",
    "漏洞",
    "cve",
    "weather",
    "forecast",
    "temperature",
    "air quality",
    "news",
    "earthquake",
    "accident",
    "explosion",
    "flight",
    "train",
    "score",
    "schedule",
    "ranking",
    "stock",
    "price",
    "exchange rate",
    "crypto",
    "earnings",
    "interest rate",
    "tariff",
    "sanction",
    "policy",
    "regulation",
    "law",
    "election",
    "poll",
    "president",
    "prime minister",
    "ceo",
    "visit",
    "meeting",
    "release",
    "version",
    "changelog",
    "vulnerability",
)

_EVENT_ACTION_TERMS: tuple[str, ...] = (
    "访问",
    "访华",
    "访美",
    "会见",
    "会晤",
    "谈判",
    "宣布",
    "官宣",
    "发布",
    "辞职",
    "任命",
    "当选",
    "去世",
    "起诉",
    "爆发",
    "冲突",
    "visit",
    "meet",
    "announce",
    "release",
    "resign",
    "appoint",
    "elected",
    "died",
    "lawsuit",
)

_LOCAL_WORKSPACE_TERMS: tuple[str, ...] = (
    "这个项目",
    "当前项目",
    "代码",
    "文件",
    "仓库",
    "workspace",
    "repo",
    "repository",
    "codebase",
    "this project",
    "this repo",
)

_SEARCH_TOOL_NAMES = ("mcp__websearch__search", "web_search")
_FETCH_TOOL_NAMES = ("mcp__websearch__fetch_page", "web_fetch")


@dataclass(frozen=True)
class RealtimePrefetchPlan:
    """Plan returned by RealtimeSearchPolicy.plan_prefetch()."""

    prefetch_calls: list[ToolCallEvent] = field(default_factory=list)
    followup_builder: Callable[[ToolResult], ToolCallEvent | None] | None = None

    @classmethod
    def empty(cls) -> RealtimePrefetchPlan:
        return cls(prefetch_calls=[], followup_builder=None)


class RealtimeSearchPolicy(Protocol):
    """Protocol for realtime search prefetch and system hint injection."""

    def plan_prefetch(
        self,
        user_message: str,
        tool_schemas: list[Any],
        state: AgentState,
    ) -> RealtimePrefetchPlan: ...

    def build_system_hint(
        self,
        user_message: str,
        tool_schemas: list[Any],
        state: AgentState,
    ) -> str: ...


def _has_tool(tool_schemas: list[Any], tool_name: str) -> bool:
    for schema in tool_schemas:
        if not isinstance(schema, dict):
            continue
        func = schema.get("function", {})
        if isinstance(func, dict) and func.get("name") == tool_name:
            return True
    return False


def _first_available_tool(tool_schemas: list[Any], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if _has_tool(tool_schemas, candidate):
            return candidate
    return None


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _has_year_or_relative_date(text: str) -> bool:
    return bool(
        re.search(r"20\d{2}(?:\s*[年/-]\s*\d{1,2}(?:\s*[月/-]\s*\d{1,2}\s*日?)?)?", text)
        or re.search(r"\b(q[1-4]|h[12])\s*20\d{2}\b", text)
        or re.search(r"\b(next|last)\s+(week|month|year|quarter)\b", text)
    )


def _looks_like_local_workspace_request(lowered: str) -> bool:
    if not _contains_any(lowered, _LOCAL_WORKSPACE_TERMS):
        return False
    # User explicitly asked for outside/current information; do search.
    if _contains_any(lowered, _EXPLICIT_SEARCH_TERMS) or any(term in lowered for term in ("新闻", "网上", "联网", "web")):
        return False
    return True


def _needs_realtime_search(user_message: str) -> bool:
    lowered = user_message.lower()
    if _looks_like_local_workspace_request(lowered):
        return False
    if _contains_any(lowered, _EXPLICIT_SEARCH_TERMS):
        return True
    has_temporal = _contains_any(lowered, _TEMPORAL_TERMS) or _has_year_or_relative_date(lowered)
    has_domain = _contains_any(lowered, _REALTIME_DOMAINS)
    if has_temporal and (has_domain or _contains_any(lowered, _EVENT_ACTION_TERMS)):
        return True
    if has_domain and any(word in lowered for word in ("怎么样", "如何", "多少", "有没有", "是否", "when", "how", "what", "?")):
        return True
    if _has_year_or_relative_date(lowered) and any(word in lowered for word in ("新闻", "消息", "通报", "news", "update")):
        return True
    return False


def _has_successful_web_result(state: AgentState) -> bool:
    valid_names = set(_SEARCH_TOOL_NAMES) | set(_FETCH_TOOL_NAMES)
    return any(
        tool_call.tool_name in valid_names and tool_call.status == "success"
        for tool_call in state.tool_calls
    )


def _has_successful_fetch_result(state: AgentState) -> bool:
    return any(
        tool_call.tool_name in _FETCH_TOOL_NAMES and tool_call.status == "success"
        for tool_call in state.tool_calls
    )


def _has_successful_search_result(state: AgentState) -> bool:
    return any(
        tool_call.tool_name in _SEARCH_TOOL_NAMES and tool_call.status == "success"
        for tool_call in state.tool_calls
    )


def _extract_first_url(text: str) -> str | None:
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


class DefaultRealtimeSearchPolicy:
    """Default implementation for realtime-search prefetch."""

    def plan_prefetch(
        self,
        user_message: str,
        tool_schemas: list[Any],
        state: AgentState,
    ) -> RealtimePrefetchPlan:
        return RealtimePrefetchPlan.empty()

    def build_system_hint(
        self,
        user_message: str,
        tool_schemas: list[Any],
        state: AgentState,
    ) -> str:
        search_tool = _first_available_tool(tool_schemas, _SEARCH_TOOL_NAMES)
        if not search_tool or not _needs_realtime_search(user_message):
            return ""

        fetch_tool = _first_available_tool(tool_schemas, _FETCH_TOOL_NAMES)
        if _has_successful_fetch_result(state):
            hint = (
                "This request needs realtime information. "
                "You already have fetched page evidence. "
                "Answer directly from the tool results. "
                "Do not claim you cannot browse or that search is unavailable. "
                "绝不能声称没有搜索工具、不能联网或无法获取实时信息。"
            )
            return hint

        if fetch_tool and _has_successful_search_result(state):
            return (
                "This request needs realtime information. "
                "You have candidate web search results only. "
                f"Do not treat search snippets as verified facts; call `{fetch_tool}` "
                "on one or more relevant source URLs before giving a factual final answer. "
                "If every fetch fails, say the evidence is insufficient instead of guessing. "
                "搜索结果只是候选来源，不能直接当作事实；请先抓取可信来源正文。"
            )

        hint = (
            "This request needs realtime information. "
            f"You must call `{search_tool}` first. "
            "Do not ask the user again for permission to browse, and do not say you cannot access realtime data. "
            "绝不能声称没有搜索工具、不能联网或无法获取实时信息。"
        )
        if fetch_tool:
            hint += (
                f" Then call `{fetch_tool}` on a relevant source URL before a factual final answer. "
                "Search snippets are candidate sources, not verified facts."
            )
        return hint
