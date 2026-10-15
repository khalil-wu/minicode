"""Exact synchronous Guardian output and completion contracts from codexsrc."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

GUARDIAN_REVIEW_TIMEOUT = 90.0
GUARDIAN_MAX_ATTEMPTS = 3
GUARDIAN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "user_authorization": {"type": "string", "enum": ["unknown", "low", "medium", "high"]},
        "outcome": {"type": "string", "enum": ["allow", "deny"]},
        "rationale": {"type": "string"},
    }, "required": ["outcome"],
}
GUARDIAN_OUTPUT_CONTRACT = '''You may use read-only tool checks to gather any additional context you need before deciding. When you are ready to answer, your final message must be strict JSON.

For low-risk actions, give the final answer directly: {"outcome":"allow"}.

For anything else, use this JSON schema:
{
  "risk_level": "low" | "medium" | "high" | "critical",
  "user_authorization": "unknown" | "low" | "medium" | "high",
  "outcome": "allow" | "deny",
  "rationale": string
}'''
REVIEW_FAILURE_INSTRUCTIONS = (
    "The action was not executed because automatic approval review could not be completed. "
    "This is a review failure, not a determination that the action is unsafe. "
    "Do not bypass the approval check; resolve the error or ask the user for guidance."
)
INPUT_BUDGET_MESSAGE = "the complete action and minimum review context exceed the reviewer input budget"


@dataclass(frozen=True)
class GuardianAssessment:
    risk_level: str
    user_authorization: str
    outcome: Literal["allow", "deny"]
    rationale: str


class GuardianReviewError(RuntimeError):
    def __init__(self, kind: str, message: str = "", *, retryable: bool = False, retry_at: float | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.retryable = retryable
        self.retry_at = retry_at


def parse_guardian_assessment(text: str | None) -> GuardianAssessment:
    if text is None:
        raise GuardianReviewError("parse", "guardian review completed without an assessment payload", retryable=True)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or start >= end:
            raise GuardianReviewError("parse", "guardian assessment was not valid JSON", retryable=True)
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise GuardianReviewError("parse", str(exc), retryable=True) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("outcome"), str) or payload["outcome"] not in {"allow", "deny"}:
        raise GuardianReviewError("parse", "guardian assessment has no valid outcome", retryable=True)
    outcome = payload["outcome"]
    risk = payload.get("risk_level")
    risk = ("low" if outcome == "allow" else "high") if risk is None else risk
    authorization = payload.get("user_authorization")
    authorization = "unknown" if authorization is None else authorization
    if not isinstance(risk, str) or risk not in {"low", "medium", "high", "critical"} or not isinstance(authorization, str) or authorization not in {"unknown", "low", "medium", "high"}:
        raise GuardianReviewError("parse", "guardian assessment has invalid risk or authorization", retryable=True)
    rationale = payload.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        raise GuardianReviewError("parse", "guardian assessment rationale must be a string", retryable=True)
    if rationale is None or not rationale.strip():
        rationale = ("Auto-review returned a low-risk allow decision." if outcome == "allow"
                     else "Auto-review returned a deny decision without a rationale.")
    return GuardianAssessment(risk, authorization, outcome, rationale)


@dataclass(frozen=True)
class GuardianReceipt:
    review_id: str
    request_digest: str
    policy_identity: str
    turn_id: str
    environment_id: str | None
    status: str
    assessment: GuardianAssessment | None = None
    rationale: str | None = None
    require_user: bool = False
    authorization_version: tuple[int, bool] | None = None
    review_context_revision: int | None = None
    history_reset_version: int | None = None

    def approval_response(self) -> dict[str, Any] | None:
        if self.require_user:
            return None
        return {"action": "approve" if self.status == "approved" else "cancel" if self.status == "aborted" else "reject",
                "request_digest": self.request_digest, "policy_identity": self.policy_identity,
                "guidance": self.rationale or "", "guardian_review_id": self.review_id,
                "guardian_status": self.status}
