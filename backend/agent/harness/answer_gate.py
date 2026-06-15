from __future__ import annotations

import re
from typing import Any

from backend.agent.harness.contracts import AnswerGateResult


DELIVERY_REQUEST_RE = re.compile(
    r"(write|create|edit|modify|update|delete|rename|run|test|build|install|"
    r"implement|fix|generate|save|"
    r"\u5199|\u521b\u5efa|\u4fee\u6539|\u66f4\u65b0|\u5220\u9664|\u91cd\u547d\u540d|"
    r"\u8fd0\u884c|\u6d4b\u8bd5|\u6784\u5efa|\u5b89\u88c5|\u5b9e\u73b0|\u4fee\u590d|"
    r"\u751f\u6210|\u4fdd\u5b58)",
    re.I,
)
INTENTION_ONLY_RE = re.compile(
    r"^\s*(i(?:'ll| will| am going to)\s+(?:do|check|look|try|work|handle|take|get|start|begin|make|create|write|edit|run|search|find|fix|implement|generate)\s|"
    r"\u6211\u4f1a|\u6211\u5c06|\u6211\u6765|\u6211\u5148|\u63a5\u4e0b\u6765\u6211)",
    re.I,
)
REALTIME_REQUEST_RE = re.compile(
    r"\b(today|latest|current|now|recent|newest|most recent|up[- ]to[- ]date|breaking|this week|tomorrow|yesterday)\b|"
    r"(\u4eca\u5929|\u6700\u65b0|\u5f53\u524d|\u73b0\u5728|\u8fd1\u671f|\u521a\u521a|\u6628\u5929|\u660e\u5929)",
    re.I,
)
WEATHER_REQUEST_RE = re.compile(
    r"\b(weather|forecast|temperature)\b|"
    r"(\u5929\u6c14|\u6c14\u6e29|\u6e29\u5ea6|\u9884\u62a5|\u964d\u96e8|\u4e0b\u96e8)",
    re.I,
)
LOCATION_HINT_RE = re.compile(
    r"\b(?:in|at|for|near)\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\b|"
    r"\b(?:Beijing|Shanghai|Guangzhou|Shenzhen|Hangzhou|Chengdu|Wuhan|Nanjing|Tianjin|Chongqing|"
    r"Tokyo|Seoul|London|Paris|Berlin|New York|San Francisco|Los Angeles|Singapore)\b|"
    r"(\u5317\u4eac|\u4e0a\u6d77|\u5e7f\u5dde|\u6df1\u5733|\u676d\u5dde|\u6210\u90fd|\u6b66\u6c49|"
    r"\u5357\u4eac|\u5929\u6d25|\u91cd\u5e86|\u82cf\u5dde|\u897f\u5b89|\u957f\u6c99|\u4e1c\u4eac|"
    r"\u9996\u5c14|\u4f26\u6566|\u5df4\u9ece|\u7ebd\u7ea6|\u65b0\u52a0\u5761)",
    re.I,
)
LOCATION_CLARIFICATION_RE = re.compile(
    r"\b(?:which|what)\b.{0,40}\b(?:city|location|area)\b|"
    r"\b(?:city|location|area)\b.{0,40}\?|"
    r"(\u54ea\u4e2a|\u54ea\u91cc|\u4ec0\u4e48).{0,24}(\u57ce\u5e02|\u5730\u70b9|\u5730\u533a)|"
    r"(\u57ce\u5e02|\u5730\u70b9|\u5730\u533a).{0,24}(\u54ea\u4e2a|\u54ea\u91cc|\u4ec0\u4e48)",
    re.I,
)
DEICTIC_LOCATION_RE = re.compile(
    r"\b(?:near me|nearby|around me|around here|close by|in my area)\b|"
    r"(\u9644\u8fd1|\u5468\u8fb9|\u8eab\u8fb9|\u8fd9\u9644\u8fd1)",
    re.I,
)
LOCAL_RECOMMENDATION_RE = re.compile(
    r"\b(?:restaurant|food|eat|meal|cafe|coffee|hotel|store|shop|things to do)\b|"
    r"(\u597d\u5403|\u5403\u7684|\u9910\u5385|\u996d\u5e97|\u5496\u5561|\u9152\u5e97|"
    r"\u8d85\u5e02|\u5546\u5e97|\u666f\u70b9|\u53bb\u54ea|\u73a9)",
    re.I,
)
ABSOLUTE_DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"\d{1,2},\s+(?:19|20)\d{2}\b|"
    r"\b\d{1,2}\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?:19|20)\d{2}\b",
    re.I,
)
FAILURE_ACK_MARKERS = (
    "\u5931\u8d25",
    "\u65e0\u6cd5",
    "\u672a\u80fd",
    "\u4e0d\u80fd",
    "\u51fa\u9519",
    "\u9519\u8bef",
    "\u88ab\u62d2\u7edd",
    "\u6743\u9650",
    "blocked",
    "failed",
    "failure",
    "error",
    "could not",
    "cannot",
    "can't",
    "unable",
    "permission",
)
LIMITATION_MARKERS = FAILURE_ACK_MARKERS + (
    "not enough",
    "not verified",
    "unverified",
    "insufficient",
    "i don't have",
)
UNCERTAINTY_MARKERS = (
    "unverified search",
    "search snippets",
    "candidate",
    "may",
    "might",
    "could",
    "appears",
    "seems",
    "likely",
    "possibly",
    "not verified",
    "unverified",
    "\u53ef\u80fd",
    "\u4f3c\u4e4e",
    "\u672a\u9a8c\u8bc1",
    "\u5019\u9009",
)
WRITE_SUCCESS_MARKERS = (
    "\u5df2\u521b\u5efa",
    "\u5df2\u5199\u5165",
    "\u5df2\u4fee\u6539",
    "\u5df2\u66f4\u65b0",
    "created",
    "wrote",
    "updated",
    "modified",
)
NONFATAL_ERROR_KINDS = {
    "missing_generated_content",
    "routing_error",
    "stale_evidence",
    "repeat_guard",
    "tool_disabled",
}
NONFATAL_PROJECTIONS = {"silent", "status", "warning"}
MUTATION_TOOLS = {"write_file", "edit_file"}


def user_message_missing_required_location(user_message: str) -> bool:
    """Return true when using tools would require a location the user omitted."""
    text = user_message or ""
    if LOCATION_HINT_RE.search(text):
        return False
    if WEATHER_REQUEST_RE.search(text):
        return True
    return bool(DEICTIC_LOCATION_RE.search(text) and LOCAL_RECOMMENDATION_RE.search(text))


class AnswerGate:
    """Final-answer integrity checks owned by the runtime harness."""

    def __init__(self, max_retries: int = 2, *, enabled: bool = True) -> None:
        self.max_retries = max_retries
        self.enabled = enabled

    def evaluate(self, user_message: str, draft_reply: str, state: Any) -> AnswerGateResult:
        if not self.enabled:
            return AnswerGateResult(ok=True)
        retry_count = int(getattr(state, "answer_gate_retries", 0) or 0)
        if retry_count >= self.max_retries:
            return AnswerGateResult(ok=True)

        text = (draft_reply or "").strip()
        if not text:
            return AnswerGateResult(ok=True)

        if self._weather_request_missing_location(user_message or "", text, state):
            return self._retry(
                state,
                retry_count,
                reason="missing_weather_location",
                feedback=(
                    "The weather request is missing a city or area. "
                    "Ask one concise city/location question first; do not assume a location."
                ),
            )

        if self._looks_like_unexecuted_commitment(user_message or "", text, state):
            return self._retry(
                state,
                retry_count,
                reason="unexecuted_commitment",
                feedback=(
                    "The draft only promises or plans the work without executing it. "
                    "If tools can complete it, call them now. Otherwise, state the concrete limitation."
                ),
            )

        if self._claims_success_without_mutation(text, state):
            return self._retry(
                state,
                retry_count,
                reason="unverified_file_change",
                feedback=(
                    "Do not claim a file was created or edited without a successful "
                    "write_file/edit_file result. Perform the write or say no file was written."
                ),
            )

        if self._unacknowledged_recent_tool_failure(text, state):
            failure = self._last_fatal_tool_failure(state)
            return self._retry(
                state,
                retry_count,
                reason="unacknowledged_tool_failure",
                feedback=(
                    "A recent tool failure must be acknowledged before giving a final answer. "
                    f"Tool: {self._record_field(failure, 'tool_name') or 'unknown'}; "
                    f"kind: {self._record_field(failure, 'error_kind') or 'unknown'}; "
                    f"reason: {self._record_field(failure, 'tool_output') or self._record_field(failure, 'user_summary') or 'no details'}."
                ),
            )

        if self._candidate_only_overclaim(text, state):
            return self._retry(
                state,
                retry_count,
                reason="candidate_evidence_overclaim",
                feedback=(
                    "The draft makes confident factual claims from candidate snippets only. "
                    "Fetch a source before confident claims, or clearly state that the answer is based on unverified search snippets."
                ),
            )

        if self._missing_absolute_date(user_message or "", text, state):
            return self._retry(
                state,
                retry_count,
                reason="missing_absolute_date",
                feedback=(
                    "This is a current/time-sensitive answer backed by fetched web evidence. "
                    "Include an absolute date, for example 2026-06-03, in the final answer."
                ),
            )

        return AnswerGateResult(ok=True)

    @staticmethod
    def _retry(state: Any, retry_count: int, *, reason: str, feedback: str) -> AnswerGateResult:
        setattr(state, "answer_gate_retries", retry_count + 1)
        return AnswerGateResult(ok=False, reason=reason, feedback=feedback)

    @classmethod
    def _weather_request_missing_location(cls, user_message: str, text: str, state: Any) -> bool:
        if not user_message_missing_required_location(user_message):
            return False
        # Once weather/location data has actually been fetched, the missing
        # location no longer blocks an answer — the agent already resolved it
        # (e.g. via geolocation or the fetched source). Re-asking the user for a
        # city after the data is in hand is a regression, and it would mask the
        # downstream absolute-date check for the now-grounded reply.
        if cls._has_fetched_web_evidence(state):
            return False
        if LOCATION_CLARIFICATION_RE.search(text or ""):
            return False
        return True

    @staticmethod
    def _records(state: Any) -> list[Any]:
        records = getattr(state, "tool_calls", []) or []
        return list(records)

    @staticmethod
    def _record_field(record: Any, field: str) -> str:
        if record is None:
            return ""
        value = getattr(record, field, "")
        return str(value or "").strip()

    @staticmethod
    def _looks_like_unexecuted_commitment(user_message: str, text: str, state: Any) -> bool:
        if not DELIVERY_REQUEST_RE.search(user_message):
            return False
        if any(getattr(tc, "status", "") == "success" for tc in (getattr(state, "tool_calls", None) or [])):
            return False
        if not INTENTION_ONLY_RE.search(text):
            return False
        return not any(marker in text.lower() for marker in FAILURE_ACK_MARKERS)

    @classmethod
    def _claims_success_without_mutation(cls, text: str, state: Any) -> bool:
        lowered = text.lower()
        if not any(marker in lowered for marker in WRITE_SUCCESS_MARKERS):
            return False
        return not any(
            cls._record_field(record, "status") == "success"
            and cls._record_field(record, "tool_name") in MUTATION_TOOLS
            for record in cls._records(state)
        )

    @classmethod
    def _last_fatal_tool_failure(cls, state: Any) -> Any | None:
        for record in reversed(cls._records(state)):
            status = cls._record_field(record, "status")
            if status not in {"error", "failed", "blocked"}:
                continue
            if cls._record_field(record, "projection") in NONFATAL_PROJECTIONS:
                continue
            if cls._record_field(record, "error_kind") in NONFATAL_ERROR_KINDS:
                continue
            return record
        return None

    @classmethod
    def _unacknowledged_recent_tool_failure(cls, text: str, state: Any) -> bool:
        if cls._last_fatal_tool_failure(state) is None:
            return False
        lowered = text.lower()
        return not any(marker in lowered for marker in FAILURE_ACK_MARKERS)

    @classmethod
    def _has_fetched_web_evidence(cls, state: Any) -> bool:
        return any(
            cls._record_field(record, "status") == "success"
            and (
                cls._record_field(record, "evidence_type") == "fetched"
                or cls._record_field(record, "tool_name") == "web_fetch"
            )
            for record in cls._records(state)
        )

    @classmethod
    def _has_candidate_evidence(cls, state: Any) -> bool:
        return any(
            cls._record_field(record, "status") == "success"
            and cls._record_field(record, "evidence_type") == "candidate"
            for record in cls._records(state)
        )

    @classmethod
    def _candidate_only_overclaim(cls, text: str, state: Any) -> bool:
        if not cls._has_candidate_evidence(state) or cls._has_fetched_web_evidence(state):
            return False
        lowered = text.lower()
        if any(marker in lowered for marker in UNCERTAINTY_MARKERS):
            return False
        if any(marker in lowered for marker in LIMITATION_MARKERS):
            return False
        return True

    @classmethod
    def _missing_absolute_date(cls, user_message: str, text: str, state: Any) -> bool:
        if not cls._has_fetched_web_evidence(state):
            return False
        if not REALTIME_REQUEST_RE.search(user_message):
            return False
        lowered = text.lower()
        if any(marker in lowered for marker in LIMITATION_MARKERS):
            return False
        return ABSOLUTE_DATE_RE.search(text) is None
