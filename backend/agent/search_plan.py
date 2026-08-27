from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.tools.contracts import SearchPlan


RELATIVE_TIME_RE = re.compile(
    r"(今天|今日|现在|当前|最新|最近|近况|实时|本周|"
    r"today|latest|current|recent|now|real[-\s]?time|this\s+week)",
    re.I,
)

ABSOLUTE_DATE_RE = re.compile(
    r"\b20\d{2}-\d{1,2}-\d{1,2}\b|20\d{2}年\d{1,2}月\d{1,2}日"
)


def current_temporal_anchor(timezone: str = "UTC") -> tuple[str, str]:
    try:
        zone = ZoneInfo(str(timezone or "UTC"))
    except (KeyError, ValueError):
        zone = ZoneInfo("UTC")
    now = datetime.now(zone)
    return now.date().isoformat(), str(getattr(zone, "key", "UTC"))


def build_search_plan(raw_query: str, *, timezone: str = "UTC") -> SearchPlan:
    query = " ".join(str(raw_query or "").split())
    date, tz = current_temporal_anchor(timezone)
    freshness = "realtime" if RELATIVE_TIME_RE.search(query) else "stable"

    normalized = query
    required_date = None
    if freshness == "realtime" and not ABSOLUTE_DATE_RE.search(normalized):
        normalized = f"{date} {normalized}".strip()
        required_date = date

    return SearchPlan(
        raw_query=query,
        normalized_query=normalized,
        required_date=required_date,
        timezone=tz,
        freshness_window=freshness,
        reject_before=None,
    )
